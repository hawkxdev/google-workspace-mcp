"""OAuth authentication endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from google_workspace_mcp.common.config import ServiceConfig

from .state import (
    REAUTHORIZATION_REQUIRED,
    InvalidClient,
    InvalidGrant,
    InvalidTarget,
    IssuedAccessToken,
    OAuthState,
    canonicalize_resource,
)

logger = logging.getLogger(__name__)

MAX_CLIENT_ID_LENGTH = 256


# URL helpers


def protected_resource_metadata_url(resource: str) -> str:
    """Build resource metadata URL."""
    parsed = urlsplit(resource)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.fragment:
        raise ValueError(
            'resource must be an absolute HTTPS URL without a fragment'
        )
    resource_path = '' if parsed.path == '/' else parsed.path
    metadata_path = f'/.well-known/oauth-protected-resource{resource_path}'
    return urlunsplit(
        (parsed.scheme, parsed.netloc, metadata_path, parsed.query, '')
    )


def authorization_server_metadata_url(issuer: str) -> str:
    """Build server metadata URL."""
    parsed = urlsplit(issuer)
    if (
        parsed.scheme != 'https'
        or not parsed.netloc
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError(
            'issuer must be an absolute HTTPS URL without fragment or query'
        )
    issuer_path = parsed.path.rstrip('/')
    metadata_path = f'/.well-known/oauth-authorization-server{issuer_path}'
    return urlunsplit((parsed.scheme, parsed.netloc, metadata_path, '', ''))


def oauth_endpoint_urls(issuer: str) -> tuple[str, str, str]:
    """Build operational endpoint URLs."""
    _ = authorization_server_metadata_url(issuer)
    base = issuer.removesuffix('/')
    return (
        f'{base}/oauth/authorize',
        f'{base}/oauth/token',
        f'{base}/oauth/register',
    )


# Security helpers


def _text_matches(candidate: str, expected: str) -> bool:
    """Compare text values securely."""
    candidate_digest = hashlib.sha256(candidate.encode('utf-8')).digest()
    expected_digest = hashlib.sha256(expected.encode('utf-8')).digest()
    return hmac.compare_digest(candidate_digest, expected_digest)


def _valid_redirect_uri(uri: str) -> bool:
    """Validate redirect URI format."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    if parsed.fragment:
        return False
    if parsed.scheme == 'https':
        return bool(parsed.netloc)
    if parsed.scheme != 'http':
        return False
    return parsed.hostname in {'localhost', '127.0.0.1', '::1'}


def _token_response(issued: IssuedAccessToken) -> JSONResponse:
    """Build token payload response."""
    expires_in = max(0, int(issued.token.expires_at - issued.token.issued_at))
    payload: dict[str, object] = {
        'access_token': issued.access_token,
        'token_type': 'bearer',
        'expires_in': expires_in,
    }
    if issued.refresh_token:
        payload['refresh_token'] = issued.refresh_token
    return JSONResponse(payload)


def _oauth_error(
    error: str,
    description: str,
    *,
    status_code: int = 400,
) -> JSONResponse:
    """Build OAuth error response."""
    return JSONResponse(
        {'error': error, 'error_description': description},
        status_code=status_code,
    )


# OAuth endpoints


class OAuthEndpoints:
    """OAuth endpoint router."""

    def __init__(
        self,
        *,
        config: ServiceConfig,
        oauth_state: OAuthState,
        login_username: str,
        login_password: str,
        audit_writer: Callable[[dict[str, object]], None],
    ) -> None:
        """Initialize OAuth endpoints router."""
        # 1. Validate service binding
        expected_path = config.oauth_state_path.expanduser().absolute()
        expected_resource = canonicalize_resource(config.public_url)
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
        # 2. Bind endpoint dependencies
        self._config: ServiceConfig = config
        self._oauth_state = oauth_state
        self._login_username = login_username
        self._login_password = login_password
        self._audit_writer = audit_writer
        self._canonical_resource = expected_resource
        # 3. Build public endpoints
        self._protected_resource_metadata_url = (
            protected_resource_metadata_url(expected_resource)
        )
        self._authorization_server_metadata_url = (
            authorization_server_metadata_url(expected_resource)
        )
        (
            self._authorization_endpoint_url,
            self._token_endpoint_url,
            self._registration_endpoint_url,
        ) = oauth_endpoint_urls(expected_resource)
        protected_path = unquote(
            urlsplit(self._protected_resource_metadata_url).path
        )
        server_path = unquote(
            urlsplit(self._authorization_server_metadata_url).path
        )
        self._authorization_endpoint_path = urlsplit(
            self._authorization_endpoint_url
        ).path
        authorization_path = unquote(self._authorization_endpoint_path)
        token_path = unquote(urlsplit(self._token_endpoint_url).path)
        registration_path = unquote(
            urlsplit(self._registration_endpoint_url).path
        )
        # 4. Register route handlers
        self._routes = [
            Route(server_path, self.oauth_metadata, methods=['GET']),
            Route(
                protected_path,
                self.oauth_protected_resource,
                methods=['GET'],
            ),
            Route(
                authorization_path,
                self.oauth_authorize,
                methods=['GET', 'POST'],
            ),
            Route(token_path, self.oauth_token, methods=['POST']),
            Route(registration_path, self.oauth_register, methods=['POST']),
        ]

    @property
    def routes(self) -> list[Route]:
        """Return configured endpoint routes."""
        return list(self._routes)

    async def oauth_metadata(self, request: Request) -> JSONResponse:
        """Handle server metadata request."""
        base = self._canonical_resource
        return JSONResponse(
            {
                'issuer': base,
                'authorization_response_iss_parameter_supported': True,
                'authorization_endpoint': self._authorization_endpoint_url,
                'token_endpoint': self._token_endpoint_url,
                'registration_endpoint': self._registration_endpoint_url,
                'resource': base,
                'response_types_supported': ['code'],
                'grant_types_supported': [
                    'authorization_code',
                    'refresh_token',
                ],
                'code_challenge_methods_supported': ['S256'],
                'token_endpoint_auth_methods_supported': [
                    'client_secret_post'
                ],
            }
        )

    async def oauth_protected_resource(self, request: Request) -> JSONResponse:
        """Handle resource metadata request."""
        base = self._canonical_resource
        return JSONResponse(
            {
                'resource': base,
                'authorization_servers': [base],
                'bearer_methods_supported': ['header'],
            }
        )

    def _request_parameters(
        self, request: Request, form: Mapping[str, Any] | None
    ) -> dict[str, str]:
        """Extract authorization request parameters."""
        source: Mapping[str, Any] = (
            form if form is not None else request.query_params
        )
        names = (
            'response_type',
            'client_id',
            'redirect_uri',
            'state',
            'code_challenge',
            'code_challenge_method',
            'resource',
        )
        params = {name: str(source.get(name, '') or '') for name in names}
        if not params['code_challenge_method']:
            params['code_challenge_method'] = 'S256'
        return params

    def _validate_authorization_request(
        self, request: Request, params: Mapping[str, str]
    ) -> Response | None:
        """Validate authorization request parameters."""
        # Response type
        if params['response_type'] != 'code':
            return JSONResponse(
                {'error': 'unsupported_response_type'}, status_code=400
            )
        # Client and redirect
        client = self._oauth_state.get_client(params['client_id'])
        if client is None or client.revoked_at is not None:
            return JSONResponse({'error': 'invalid_client'}, status_code=400)
        if not self._oauth_state.client_redirect_uri_allowed(
            params['client_id'], params['redirect_uri']
        ):
            return JSONResponse(
                {
                    'error': 'invalid_request',
                    'error_description': (
                        'Invalid or unregistered redirect_uri'
                    ),
                },
                status_code=400,
            )
        # PKCE and resource
        if (
            not params['code_challenge']
            or params['code_challenge_method'] != 'S256'
        ):
            return JSONResponse(
                {
                    'error': 'invalid_request',
                    'error_description': 'PKCE with S256 is required',
                },
                status_code=400,
            )
        if (
            canonicalize_resource(params['resource'])
            != self._canonical_resource
        ):
            return JSONResponse(
                {
                    'error': 'invalid_target',
                    'error_description': 'resource must match this server',
                },
                status_code=400,
            )
        return None

    def _login_configured(self) -> bool:
        """Check configured login state."""
        return bool(self._login_username and self._login_password)

    def _check_credentials(self, username: str, password: str) -> bool:
        """Verify user login credentials."""
        return bool(
            self._login_configured()
            and _text_matches(username, self._login_username)
            and _text_matches(password, self._login_password)
        )

    def _login_form(
        self,
        params: Mapping[str, str],
        *,
        client_name: str,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """Render interactive login page."""
        # Hidden request state
        hidden = '\n'.join(
            f'<input type="hidden" name="{html.escape(name)}" '
            f'value="{html.escape(value, quote=True)}">'
            for name, value in params.items()
        )
        error_html = (
            f'<p role="alert">{html.escape(error)}</p>' if error else ''
        )
        client_name_html = html.escape(client_name)
        redirect_uri_html = html.escape(params['redirect_uri'])
        service_title = html.escape(self._config.service_id.capitalize())
        # Login page
        content = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Authorize {service_title} MCP</title></head>
<body>
  <main>
    <h1>Authorize {service_title} MCP</h1>
    <p>Client: <strong>{client_name_html}</strong></p>
    <p>Redirect URI: <code>{redirect_uri_html}</code></p>
    {error_html}
    <form method="post" action="{self._authorization_endpoint_path}">
      {hidden}
      <label>Username <input name="username" autocomplete="username"></label>
      <label>Password
        <input name="password" type="password"
          autocomplete="current-password"></label>
      <button type="submit">Authorize</button>
    </form>
  </main>
</body>
</html>"""
        return HTMLResponse(content, status_code=status_code)

    def _login_misconfigured(self) -> HTMLResponse:
        """Build misconfigured login response."""
        return HTMLResponse(
            'OAuth login is not configured.',
            status_code=503,
        )

    async def oauth_authorize(self, request: Request) -> Response:
        """Handle user authorization request."""
        # Request validation
        form = await request.form() if request.method == 'POST' else None
        params = self._request_parameters(request, form)
        invalid = self._validate_authorization_request(request, params)
        if invalid is not None:
            return invalid
        # Owner authentication
        if not self._login_configured():
            logger.error(
                'OAuth authorization refused: login is not configured'
            )
            return self._login_misconfigured()
        client = self._oauth_state.get_client(params['client_id'])
        if client is None:
            return JSONResponse({'error': 'invalid_client'}, status_code=400)
        if request.method == 'GET':
            return self._login_form(params, client_name=client.client_name)
        if form is None:
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        username = str(form.get('username', '') or '')
        password = str(form.get('password', '') or '')
        if not self._check_credentials(username, password):
            logger.warning('OAuth login failed')
            return self._login_form(
                params,
                client_name=client.client_name,
                error='Invalid username or password',
                status_code=401,
            )

        # Authorization code
        requires_reauthorization = client.policy == REAUTHORIZATION_REQUIRED
        code = self._oauth_state.issue_authorization_code(
            client_id=client.client_id,
            redirect_uri=params['redirect_uri'],
            code_challenge=params['code_challenge'],
            resource=params['resource'],
            fresh_reauthorization=requires_reauthorization,
        )
        query = {'code': code, 'iss': self._canonical_resource}
        if params['state']:
            query['state'] = params['state']
        separator = '&' if '?' in params['redirect_uri'] else '?'
        location = (
            f'{params["redirect_uri"]}{separator}'
            f'{urllib.parse.urlencode(query)}'
        )
        logger.info('OAuth authorization code issued')
        return RedirectResponse(location, status_code=302)

    async def _handle_authorization_code(
        self, request: Request, form: Mapping[str, Any]
    ) -> JSONResponse:
        """Exchange authorization code grant."""
        # Grant parameters
        fields = {
            name: str(form.get(name, '') or '')
            for name in (
                'code',
                'client_id',
                'client_secret',
                'redirect_uri',
                'code_verifier',
                'resource',
            )
        }
        if not all(fields.values()):
            return _oauth_error(
                'invalid_request',
                'code, client_id, client_secret, redirect_uri, '
                'code_verifier, and resource are required',
            )
        # Code redemption
        try:
            issued = self._oauth_state.redeem_authorization_code(**fields)
        except InvalidClient:
            return _oauth_error(
                'invalid_client',
                'client authentication failed',
                status_code=401,
            )
        except InvalidTarget:
            return _oauth_error(
                'invalid_target',
                'resource does not match authorization',
            )
        except InvalidGrant:
            return _oauth_error(
                'invalid_grant', 'authorization code is invalid'
            )
        return _token_response(issued)

    async def _handle_refresh_token(
        self, request: Request, form: Mapping[str, Any]
    ) -> JSONResponse:
        """Exchange refresh token grant."""
        # Refresh parameters
        refresh_token = str(form.get('refresh_token', '') or '')
        client_id = str(form.get('client_id', '') or '')
        resource = canonicalize_resource(str(form.get('resource', '') or ''))
        if not refresh_token or not client_id or not resource:
            return _oauth_error(
                'invalid_request',
                'refresh_token, client_id, and resource are required',
            )
        if len(client_id) > MAX_CLIENT_ID_LENGTH:
            return _oauth_error('invalid_request', 'client_id is too long')
        if resource != self._canonical_resource:
            await self._audit_refresh_failure(client_id, 'invalid_target')
            return _oauth_error(
                'invalid_target', 'resource must match this server'
            )
        # Token rotation
        try:
            issued = await run_in_threadpool(
                self._rotate_and_audit,
                refresh_token,
                client_id,
                resource,
            )
        except InvalidClient:
            await self._audit_refresh_failure(client_id, 'invalid_client')
            return _oauth_error(
                'invalid_client',
                'client authentication failed',
                status_code=401,
            )
        except InvalidTarget:
            await self._audit_refresh_failure(client_id, 'invalid_target')
            return _oauth_error(
                'invalid_target',
                'resource does not match authorization',
            )
        except InvalidGrant:
            await self._audit_refresh_failure(client_id, 'invalid_grant')
            return _oauth_error('invalid_grant', 'refresh token is invalid')
        return _token_response(issued)

    def _rotate_and_audit(
        self, refresh_token: str, client_id: str, resource: str
    ) -> IssuedAccessToken:
        """Rotate tokens with audit."""
        return self._oauth_state.redeem_refresh_token(
            refresh_token=refresh_token,
            client_id=client_id,
            resource=resource,
            audit_hook=self._audit_refresh_rotation,
        )

    def _audit_refresh_rotation(self, issued: IssuedAccessToken) -> None:
        """Record refresh rotation event."""
        token_id_hash = hashlib.sha256(
            issued.access_token.encode('utf-8')
        ).hexdigest()
        record: dict[str, object] = {
            'timestamp': datetime.now(UTC).isoformat(),
            'operation': 'oauth_refresh_rotation',
            'operation_status': 'success',
            'client_id': issued.token.client_id,
            'principal_id': f'oauth:{issued.token.client_id}',
            'auth_policy': issued.token.policy,
            'token_id_hash': token_id_hash,
        }
        self._audit_writer(record)

    async def _audit_refresh_failure(self, client_id: str, error: str) -> None:
        """Record refresh failure event."""
        record: dict[str, object] = {
            'timestamp': datetime.now(UTC).isoformat(),
            'operation': 'oauth_refresh_rotation',
            'operation_status': 'failure',
            'client_id': client_id or None,
            'principal_id': f'oauth:{client_id}' if client_id else None,
            'error': error,
        }
        await self._write_audit(record)

    async def _write_audit(self, record: dict[str, object]) -> None:
        """Safely write audit record."""
        await asyncio.to_thread(self._audit_writer, record)

    async def oauth_token(self, request: Request) -> JSONResponse:
        """Handle token exchange request."""
        form = await request.form()
        grant_type = str(form.get('grant_type', '') or '')
        if grant_type == 'authorization_code':
            return await self._handle_authorization_code(request, form)
        if grant_type == 'refresh_token':
            return await self._handle_refresh_token(request, form)
        return _oauth_error('unsupported_grant_type', 'unsupported grant_type')

    async def oauth_register(self, request: Request) -> JSONResponse:
        """Handle dynamic client registration."""
        # Registration metadata
        try:
            body = await request.json()
        except ValueError, TypeError:
            return _oauth_error('invalid_client_metadata', 'invalid JSON body')
        if not isinstance(body, dict):
            return _oauth_error(
                'invalid_client_metadata',
                'registration body must be an object',
            )
        raw_redirects = body.get('redirect_uris', [])
        if not isinstance(raw_redirects, list):
            return _oauth_error(
                'invalid_client_metadata',
                'redirect_uris must be an array',
            )
        if not raw_redirects or any(
            not isinstance(uri, str) or not _valid_redirect_uri(uri)
            for uri in raw_redirects
        ):
            return _oauth_error(
                'invalid_redirect_uri',
                'redirect_uris must contain only HTTPS or loopback HTTP URIs',
            )
        redirect_uris = list(dict.fromkeys(raw_redirects))
        client_name = body.get('client_name', 'Google Workspace MCP Client')
        if not isinstance(client_name, str):
            return _oauth_error(
                'invalid_client_metadata', 'client_name must be a string'
            )
        # Client registration
        registered = self._oauth_state.register_client(
            redirect_uris, client_name=client_name
        )
        return JSONResponse(
            {
                'client_id': registered.client.client_id,
                'client_secret': registered.client_secret,
                'client_name': registered.client.client_name,
                'grant_types': ['authorization_code'],
                'response_types': ['code'],
                'redirect_uris': list(registered.client.redirect_uris),
                'token_endpoint_auth_method': 'client_secret_post',
            },
            status_code=201,
        )
