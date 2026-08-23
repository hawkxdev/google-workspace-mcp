"""Transport service factory."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Sequence

from starlette.applications import Starlette

from google_workspace_mcp.audit.logger import validate_audit_path
from google_workspace_mcp.auth.oauth import protected_resource_metadata_url
from google_workspace_mcp.auth.state import OAuthState, canonicalize_resource
from google_workspace_mcp.common.config import (
    ServiceConfig,
    validate_forwarded_allow_ips,
)
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.server import build_app

logger = logging.getLogger(__name__)


def _validate_public_url_is_https(config: ServiceConfig) -> None:
    """Validate public HTTPS resource."""
    protected_resource_metadata_url(canonicalize_resource(config.public_url))


def _validate_state_paths_are_distinct(config: ServiceConfig) -> None:
    """Validate distinct state paths."""
    audit = os.path.abspath(config.audit_log_path)
    if audit == os.path.abspath(config.oauth_state_path):
        raise ValueError('audit_log_path and oauth_state_path must differ')
    if audit == os.path.abspath(config.google_token_path):
        raise ValueError('audit_log_path and google_token_path must differ')


def validate_service_config(config: ServiceConfig) -> None:
    """Validate runtime service configuration."""
    _validate_public_url_is_https(config)
    validate_forwarded_allow_ips(
        config.forwarded_allow_ips, config.service_id.upper()
    )
    validate_audit_path(config.audit_log_path, config.download_path)
    _validate_state_paths_are_distinct(config)


def _shutdown_extensions(extensions: Sequence[Extension]) -> None:
    """Release prepared extension resources."""
    for ext in reversed(tuple(extensions)):
        with contextlib.suppress(Exception):
            ext.shutdown()


def create_service_app(
    config: ServiceConfig,
    extensions: Sequence[Extension] = (),
) -> tuple[Starlette, PolicyMCPServer, OAuthState]:
    """Create service application."""
    validate_service_config(config)
    server = PolicyMCPServer(name=config.service_id)
    registrar = ToolRegistrar(server)
    prepared: list[Extension] = []
    try:
        # Tool registration
        for ext in extensions:
            ext.register_tools(registrar)
            prepared.append(ext)
        state = OAuthState(
            config.oauth_state_path,
            download_path=config.download_path,
            service_id=config.service_id,
            resource=config.public_url,
            readonly_capabilities=server.registered_capabilities(),
            legacy_path=config.legacy_clients_path,
            approved_legacy_client_ids=config.approved_legacy_client_ids,
            access_token_ttl_seconds=config.access_token_ttl_seconds,
            refresh_token_ttl_seconds=config.refresh_token_ttl_seconds,
        )
    except BaseException:
        _shutdown_extensions(prepared)
        raise
    try:
        # Transport assembly
        app = build_app(config, state, server, extensions=extensions)
        return app, server, state
    except BaseException:
        state.close()
        _shutdown_extensions(extensions)
        raise
