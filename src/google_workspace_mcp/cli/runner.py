"""CLI service runner module."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import uvicorn
from starlette.applications import Starlette

from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport.authorization import PolicyMCPServer
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.factory import validate_service_config


def run_service_server(
    service_name: str,
    factory: Callable[
        [ServiceConfig | None, Sequence[Extension]],
        tuple[Starlette, PolicyMCPServer, OAuthState],
    ],
    *,
    extensions: Sequence[Extension] = (),
) -> None:
    """Run service ASGI server."""
    cfg = ServiceConfig.from_env(service_name)
    validate_service_config(cfg)
    app, _, state = factory(cfg, extensions)
    try:
        uvicorn.run(
            app,
            host=cfg.host,
            port=cfg.port,
            proxy_headers=True,
            forwarded_allow_ips=list(cfg.forwarded_allow_ips),
        )
    finally:
        state.close()
