"""Streamable HTTP transport assembly."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Match, Mount, Route, WebSocketRoute

from google_workspace_mcp.audit.logger import AuditLogger
from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.oauth import OAuthEndpoints
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport.authorization import PolicyMCPServer
from google_workspace_mcp.transport.extensions import Extension

logger = logging.getLogger(__name__)


def _covers(route: BaseRoute, method: str, path: str) -> Match:
    """Inspect route match."""
    try:
        match, _ = route.matches(
            {'type': 'http', 'method': method, 'path': path, 'headers': []}
        )
    except Exception as exc:
        route_path = getattr(route, 'path', route)
        raise ValueError(
            f'extension route {route_path!r} could not be safely inspected'
        ) from exc
    return match


def _validate_extension_routes(
    ext_routes: Sequence[BaseRoute],
    *,
    mcp_path: str,
) -> None:
    """Validate extension route safety."""
    exempt_paths = (
        '/health',
        '/.well-known/oauth-authorization-server',
        '/.well-known/oauth-protected-resource',
        '/oauth/authorize',
        '/oauth/token',
        '/oauth/register',
    )
    exempt_method_paths = (
        (('GET', '/'), ('HEAD', '/')) if mcp_path != '/' else ()
    )
    for r in ext_routes:
        if isinstance(r, (Mount, WebSocketRoute)):
            raise ValueError(
                f'extension {type(r).__name__} is not allowed: it can serve '
                'an unauthenticated surface -- register plain HTTP Routes'
            )
        for p in exempt_paths:
            if _covers(r, 'GET', p) is not Match.NONE:
                raise ValueError(
                    f'extension route {getattr(r, "path", r)!r} covers '
                    f'auth-exempt path {p!r}; it would be served without '
                    'bearer authentication'
                )
        for m, p in exempt_method_paths:
            if _covers(r, m, p) is Match.FULL:
                raise ValueError(
                    f'extension route {getattr(r, "path", r)!r} covers '
                    f'auth-exempt {m} {p!r}; it would be served without '
                    'bearer authentication'
                )
        if _covers(r, 'POST', mcp_path) is not Match.NONE:
            raise ValueError(
                f'extension route {getattr(r, "path", r)!r} covers MCP '
                f'transport path {mcp_path!r}; it would bypass MCP '
                'authorization'
            )


def build_app(
    config: ServiceConfig,
    state: OAuthState,
    server: PolicyMCPServer,
    extensions: Sequence[Extension] = (),
) -> Starlette:
    """Assemble transport Starlette app."""
    audit = AuditLogger(config.audit_log_path)
    oauth = OAuthEndpoints(
        config=config,
        oauth_state=state,
        login_username=config.oauth_login_username,
        login_password=config.oauth_login_password,
        audit_writer=audit.log_event,
    )
    mcp_app = server.streamable_http_app(
        streamable_http_path=config.mcp_path,
        stateless_http=True,
        json_response=True,
    )

    async def health(_: Request) -> JSONResponse:
        """Return service health status."""
        return JSONResponse({'service': config.service_id, 'status': 'ok'})

    async def ready(_: Request) -> JSONResponse:
        """Return service readiness status."""
        return JSONResponse({'status': 'ready'})

    routes: list[BaseRoute] = [
        Route('/health', health, methods=['GET']),
        Route('/ready', ready, methods=['GET']),
        *oauth.routes,
        *mcp_app.routes,
    ]
    if config.mcp_path != '/':

        async def mcp_root_probe(_: Request) -> Response:
            """Handle root protocol probe."""
            return Response(
                status_code=200,
                headers={'MCP-Protocol-Version': '2025-06-18'},
            )

        routes.append(Route('/', mcp_root_probe, methods=['GET', 'HEAD']))

    extensions_tuple = tuple(extensions)

    @asynccontextmanager
    async def app_lifespan(app: Starlette) -> AsyncIterator[None]:
        """Manage application lifespan."""
        try:
            async with mcp_app.router.lifespan_context(app):
                try:
                    yield
                finally:
                    for ext in reversed(extensions_tuple):
                        try:
                            ext.shutdown()
                        except Exception:
                            logger.exception('Extension shutdown failed')
        finally:
            # Close state on shutdown
            state.close()

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                BearerAuthMiddleware,
                config=config,
                oauth_state=state,
            )
        ],
        lifespan=app_lifespan,
    )

    before_ids = {id(r) for r in app.routes}
    for ext in extensions_tuple:
        ext.register_routes(app)
    ext_routes = [r for r in app.routes if id(r) not in before_ids]
    _validate_extension_routes(ext_routes, mcp_path=config.mcp_path)

    return app
