"""Authenticate OAuth bearer requests."""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlsplit, urlunsplit

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
from .state import (
    LEGACY_FULL,
    MCP_READONLY_V1,
    OAuthState,
    canonicalize_resource,
)

_AUTH_EXEMPT_PATHS = frozenset(
    {
        '/health',
        '/.well-known/oauth-authorization-server',
        '/.well-known/oauth-protected-resource',
        '/oauth/authorize',
        '/oauth/token',
        '/oauth/register',
    }
)


def protected_resource_metadata_url(resource: str) -> str:
    """Build RFC 9728 metadata URL."""
    parsed = urlsplit(resource)
    metadata_path = f'/.well-known/oauth-protected-resource{parsed.path}'
    return urlunsplit(
        (parsed.scheme, parsed.netloc, metadata_path, parsed.query, '')
    )


def _credential_digest(token: str) -> str:
    """Derive secret-free credential identity."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _auth_error(
    metadata_url: str,
    *,
    message: str,
    status_code: int,
    error: str,
) -> JSONResponse:
    """Build OAuth bearer error."""
    challenge = (
        f'Bearer realm="mcp", resource_metadata="{metadata_url}", '
        f'error="{error}"'
    )
    return JSONResponse(
        {'error': message},
        status_code=status_code,
        headers={'WWW-Authenticate': challenge},
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
        expected_resource = canonicalize_resource(config.public_url)
        resource_metadata_url = protected_resource_metadata_url(
            expected_resource
        )
        resource_metadata_path = urlsplit(resource_metadata_url).path
        if (
            config.mcp_path in _AUTH_EXEMPT_PATHS
            or config.mcp_path == resource_metadata_path
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
        self._resource_metadata_path = resource_metadata_path
        self._readonly_capabilities = frozenset(
            oauth_state.readonly_capabilities
        )

    def _authenticate_bearer(
        self, token: str
    ) -> AuthenticatedPrincipal | None:
        """Authenticate one bearer token."""
        if not token.startswith('v1.'):
            return None
        metadata = self._oauth_state.lookup_access_token(token)
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
        if path in _AUTH_EXEMPT_PATHS or path == self._resource_metadata_path:
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
        auth_header = request.headers.get('Authorization', '')
        auth_parts = auth_header.split(maxsplit=1)
        if (
            len(auth_parts) != 2
            or auth_parts[0].lower() != 'bearer'
            or not auth_parts[1]
        ):
            return _auth_error(
                self._resource_metadata_url,
                message='Missing or malformed Authorization header',
                status_code=401,
                error='invalid_request',
            )
        principal = self._authenticate_bearer(auth_parts[1])
        if principal is None:
            return _auth_error(
                self._resource_metadata_url,
                message='Invalid token',
                status_code=401,
                error='invalid_token',
            )
        if (
            not principal.full_access
            and request.url.path != self._config.mcp_path
        ):
            return _auth_error(
                self._resource_metadata_url,
                message='Insufficient scope',
                status_code=403,
                error='insufficient_scope',
            )
        context_token = set_request_context(
            principal=principal,
            request_id=uuid.uuid4().hex,
        )
        try:
            return await call_next(request)
        finally:
            reset_request_context(context_token)
