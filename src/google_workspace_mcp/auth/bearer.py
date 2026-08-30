"""Authenticate OAuth bearer requests."""

from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import unquote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from google_workspace_mcp.common.config import ServiceConfig

from .context import (
    AuthenticatedPrincipal,
    reset_request_context,
    set_request_context,
)
from .oauth import (
    authorization_server_metadata_url,
    oauth_endpoint_urls,
    protected_resource_metadata_url,
)
from .state import (
    LEGACY_FULL,
    MCP_READONLY_V1,
    OAuthState,
)

_AUTH_EXEMPT_PATHS = frozenset({'/health'})


def public_request_paths(issuer: str, resource: str) -> tuple[str, ...]:
    """List unauthenticated request paths."""
    scoped = (
        protected_resource_metadata_url(resource),
        authorization_server_metadata_url(issuer),
        *oauth_endpoint_urls(issuer),
    )
    paths = {*_AUTH_EXEMPT_PATHS}
    for url in scoped:
        path = urlsplit(url).path
        paths.add(path)
        paths.add(unquote(path))
    return tuple(sorted(paths))


_BEARER_CREDENTIALS = re.compile(
    r'(?i:Bearer) +(?P<token>[A-Za-z0-9\-._~+/]+=*)\Z'
)


def _credential_digest(token: str) -> str:
    """Derive secure credential identity."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _auth_error(
    metadata_url: str,
    *,
    status_code: int,
    scope: str | None,
    message: str | None = None,
    error: str | None = None,
) -> Response:
    """Build OAuth bearer response."""
    parameters = [
        'realm="mcp"',
        f'resource_metadata="{metadata_url}"',
    ]
    if scope is not None:
        parameters.append(f'scope="{scope}"')
    if error is not None:
        parameters.append(f'error="{error}"')
    headers = {'WWW-Authenticate': f'Bearer {", ".join(parameters)}'}
    if message is None:
        return Response(status_code=status_code, headers=headers)
    return JSONResponse(
        {'error': message},
        status_code=status_code,
        headers=headers,
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate OAuth bearer tokens."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: ServiceConfig,
        oauth_state: OAuthState,
    ) -> None:
        """Configure bearer authentication."""
        super().__init__(app)
        expected_path = config.oauth_state_path.expanduser().absolute()
        expected_issuer = config.public_url
        expected_resource = config.resource_url
        resource_metadata_url = protected_resource_metadata_url(
            expected_resource
        )
        server_metadata_url = authorization_server_metadata_url(
            expected_issuer
        )
        resource_metadata_path = urlsplit(resource_metadata_url).path
        server_metadata_path = urlsplit(server_metadata_url).path
        oauth_paths = tuple(
            urlsplit(url).path for url in oauth_endpoint_urls(expected_issuer)
        )
        public_routing_paths = {
            unquote(resource_metadata_path),
            unquote(server_metadata_path),
            *(unquote(path) for path in oauth_paths),
        }
        if (
            config.mcp_path in _AUTH_EXEMPT_PATHS
            or config.mcp_path in public_routing_paths
            or config.mcp_path == '/ready'
            or any(
                config.mcp_path == prefix
                or config.mcp_path.startswith(prefix + '/')
                for prefix in ('/oauth', '/.well-known')
            )
        ):
            raise ValueError('MCP path collides with a public auth route')
        if oauth_state.path != expected_path:
            raise ValueError('OAuth state path does not match ServiceConfig')
        if oauth_state.service_id != config.service_id:
            raise ValueError(
                'OAuth state service does not match ServiceConfig'
            )
        if oauth_state.resource != expected_resource:
            raise ValueError(
                'OAuth state resource does not match ServiceConfig'
            )
        self._config = config
        self._oauth_state = oauth_state
        self._resource = expected_resource
        self._resource_metadata_url = resource_metadata_url
        self._public_paths = frozenset(public_routing_paths)
        self._public_raw_paths = frozenset(
            path.encode('ascii')
            for path in (
                resource_metadata_path,
                server_metadata_path,
                *oauth_paths,
            )
        )
        self._readonly_capabilities = frozenset(
            oauth_state.readonly_capabilities
        )
        self._required_scope = ' '.join(self._readonly_capabilities) or None

    async def _authenticate_bearer(
        self, token: str
    ) -> AuthenticatedPrincipal | None:
        """Authenticate one bearer token."""
        if not token.startswith('v1.'):
            return None
        metadata = await run_in_threadpool(
            self._oauth_state.lookup_access_token, token
        )
        if metadata is None or metadata.resource != self._resource:
            return None
        capabilities = frozenset(metadata.capabilities)
        if metadata.policy == LEGACY_FULL:
            if capabilities:
                return None
            full_access = True
        elif metadata.policy == MCP_READONLY_V1:
            if capabilities != self._readonly_capabilities:
                return None
            full_access = False
        else:
            return None
        return AuthenticatedPrincipal(
            principal_id=f'oauth:{metadata.client_id}',
            credential_id=_credential_digest(token),
            client_id=metadata.client_id,
            policy=metadata.policy,
            capabilities=capabilities,
            full_access=full_access,
        )

    def _request_is_exempt(self, request: Request) -> bool:
        """Check public request routes."""
        path = request.url.path
        raw_path = request.scope.get('raw_path', path.encode('ascii'))
        if (
            path in _AUTH_EXEMPT_PATHS
            or path in self._public_paths
            or raw_path in self._public_raw_paths
        ):
            return True
        return (
            self._config.mcp_path != '/'
            and request.method in {'GET', 'HEAD'}
            and path == '/'
        )

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Authenticate one HTTP request."""
        if self._request_is_exempt(request):
            return await call_next(request)
        auth_headers = request.headers.getlist('Authorization')
        if not auth_headers:
            return _auth_error(
                self._resource_metadata_url,
                status_code=401,
                scope=self._required_scope,
            )
        if len(auth_headers) != 1:
            return _auth_error(
                self._resource_metadata_url,
                message='Malformed Authorization header',
                status_code=400,
                error='invalid_request',
                scope=self._required_scope,
            )
        auth_header = auth_headers[0]
        auth_parts = auth_header.split(maxsplit=1)
        if not auth_parts or auth_parts[0].lower() != 'bearer':
            return _auth_error(
                self._resource_metadata_url,
                status_code=401,
                scope=self._required_scope,
            )
        match = _BEARER_CREDENTIALS.fullmatch(auth_header)
        if match is None:
            return _auth_error(
                self._resource_metadata_url,
                message='Malformed Authorization header',
                status_code=400,
                error='invalid_request',
                scope=self._required_scope,
            )
        principal = await self._authenticate_bearer(match.group('token'))
        if principal is None:
            return _auth_error(
                self._resource_metadata_url,
                message='Invalid token',
                status_code=401,
                error='invalid_token',
                scope=self._required_scope,
            )
        if not principal.full_access and request.url.path not in (
            self._config.mcp_path,
            '/ready',
        ):
            return _auth_error(
                self._resource_metadata_url,
                message='Insufficient scope',
                status_code=403,
                error='insufficient_scope',
                scope=self._required_scope,
            )
        context_token = set_request_context(
            principal=principal,
            request_id=uuid.uuid4().hex,
        )
        try:
            return await call_next(request)
        finally:
            reset_request_context(context_token)
