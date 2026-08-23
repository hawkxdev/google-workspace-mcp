"""Transport service factory."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.applications import Starlette

from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.server import build_app


def create_service_app(
    config: ServiceConfig,
    extensions: Sequence[Extension] = (),
) -> tuple[Starlette, PolicyMCPServer, OAuthState]:
    """Create service application."""
    state = OAuthState(
        config.oauth_state_path,
        download_path=config.download_path,
        service_id=config.service_id,
        resource=config.public_url,
        legacy_path=config.legacy_clients_path,
        approved_legacy_client_ids=config.approved_legacy_client_ids,
        access_token_ttl_seconds=config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=config.refresh_token_ttl_seconds,
    )
    try:
        server = PolicyMCPServer(name=config.service_id)
        registrar = ToolRegistrar(server)
        for ext in extensions:
            ext.register_tools(registrar)
        app = build_app(config, state, server, extensions=extensions)
        return app, server, state
    except BaseException:
        # Close on construction failure
        state.close()
        raise
