"""Sheets service factory."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.applications import Starlette

from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport.authorization import PolicyMCPServer
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.factory import create_service_app


def create_sheets_app(
    config: ServiceConfig | None = None,
    extensions: Sequence[Extension] = (),
) -> tuple[Starlette, PolicyMCPServer, OAuthState]:
    """Create Sheets application."""
    resolved_config = config or ServiceConfig.from_env('sheets')
    return create_service_app(resolved_config, extensions=extensions)
