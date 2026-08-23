"""Test OAuth bearer authentication."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Scope

from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.context import (
    AuthenticatedPrincipal,
    current_principal,
    current_request_context,
    reset_request_context,
    set_request_context,
)
from google_workspace_mcp.auth.state import (
    LEGACY_FULL,
    MCP_READONLY_V1,
    OAuthState,
    TokenMetadata,
)
from google_workspace_mcp.common.config import ServiceConfig

RESOURCE = 'https://mcp.example.test/gmail/mcp'
FOREIGN_RESOURCE = 'https://mcp.example.test/drive/mcp'
RESOURCE_METADATA = (
    'https://mcp.example.test/.well-known/oauth-protected-resource/gmail/mcp'
)
READONLY_CAPABILITIES = ('gmail.read',)


def _service_config(state_dir: Path) -> ServiceConfig:
    """Build isolated service config."""
    return ServiceConfig(
        service_id='gmail',
        public_url=RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=state_dir / 'downloads',
        oauth_state_path=state_dir / 'oauth.sqlite3',
        google_token_path=state_dir / 'google-token.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='test-password',
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )


@pytest.fixture
def service_config(state_dir: Path) -> ServiceConfig:
    """Build service config fixture."""
    return _service_config(state_dir)


@pytest.fixture
def oauth_state(
    service_config: ServiceConfig,
    tmp_path: Path,
) -> Iterator[OAuthState]:
    """Build isolated OAuth state."""
    state = OAuthState(
        service_config.oauth_state_path,
        download_path=tmp_path / 'downloads',
        service_id=service_config.service_id,
        resource=service_config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
        access_token_ttl_seconds=service_config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=service_config.refresh_token_ttl_seconds,
    )
    try:
        yield state
    finally:
        state.close()


def _protected_app(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> Starlette:
    """Build protected test application."""

    async def protected(_: Request) -> PlainTextResponse:
        """Return authenticated principal."""
        principal = current_principal()
        return PlainTextResponse(
            principal.principal_id if principal is not None else 'missing'
        )

    app = Starlette(
        routes=[
            Route(service_config.mcp_path, protected),
            Route('/admin', protected),
        ]
    )
    app.add_middleware(
        BearerAuthMiddleware,
        config=service_config,
        oauth_state=oauth_state,
    )
    return app


def _single_route_app(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    endpoint: Callable[[Request], Awaitable[Response]],
) -> Starlette:
    """Build single route application."""
    app = Starlette(routes=[Route(service_config.mcp_path, endpoint)])
    app.add_middleware(
        BearerAuthMiddleware,
        config=service_config,
        oauth_state=oauth_state,
    )
    return app


@pytest.fixture
def client(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> Iterator[TestClient]:
    """Build protected test client."""
    with TestClient(
        _protected_app(service_config, oauth_state)
    ) as test_client:
        yield test_client


def _issue_token(
    state: OAuthState, resource: str = RESOURCE
) -> tuple[str, str]:
    """Issue an OAuth token."""
    registered = state.register_client(
        ('https://client.example.test/oauth/callback',)
    )
    issued = state.issue_access_token(
        client_id=registered.client.client_id,
        resource=resource,
    )
    return issued.access_token, registered.client.client_id


async def _asgi_request(
    app: ASGIApp,
    path: str,
    *,
    method: str = 'GET',
    authorization: str | None = None,
) -> tuple[int, bytes]:
    """Issue request in task."""
    # 1. Build request scope
    headers = []
    if authorization is not None:
        headers.append((b'authorization', authorization.encode('ascii')))
    scope: Scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'https',
        'path': path,
        'raw_path': path.encode('ascii'),
        'query_string': b'',
        'root_path': '',
        'headers': headers,
        'client': ('127.0.0.1', 50000),
        'server': ('mcp.example.test', 443),
        'state': {},
    }
    # 2. Prepare ASGI channels
    messages: list[Message] = []
    delivered = False

    async def receive() -> Message:
        """Receive one ASGI request."""
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()
        raise AssertionError('unreachable')

    async def send(message: Message) -> None:
        """Capture one ASGI response."""
        messages.append(message)

    # 3. Execute application
    await app(scope, receive, send)
    # 4. Collect response
    status = next(
        message['status']
        for message in messages
        if message['type'] == 'http.response.start'
    )
    body = b''.join(
        message.get('body', b'')
        for message in messages
        if message['type'] == 'http.response.body'
    )
    return status, body


def test_middleware_rejects_mismatched_state_path(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    mismatched = replace(
        service_config,
        oauth_state_path=service_config.oauth_state_path.with_name(
            'other.sqlite3'
        ),
    )

    with pytest.raises(ValueError, match='OAuth state path'):
        BearerAuthMiddleware(
            Starlette(), config=mismatched, oauth_state=oauth_state
        )


def test_middleware_rejects_mismatched_service(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    mismatched = replace(service_config, service_id='drive')

    with pytest.raises(ValueError, match='OAuth state service'):
        BearerAuthMiddleware(
            Starlette(), config=mismatched, oauth_state=oauth_state
        )


def test_middleware_rejects_mismatched_resource(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    mismatched = replace(service_config, public_url=FOREIGN_RESOURCE)

    with pytest.raises(ValueError, match='OAuth state resource'):
        BearerAuthMiddleware(
            Starlette(), config=mismatched, oauth_state=oauth_state
        )


@pytest.mark.parametrize(
    'mcp_path',
    [
        '/health',
        '/.well-known/oauth-authorization-server',
        '/.well-known/oauth-protected-resource',
        '/oauth/authorize',
        '/oauth/token',
        '/oauth/register',
        '/.well-known/oauth-protected-resource/gmail/mcp',
        '/.well-known/oauth-authorization-server/gmail/mcp',
        '/gmail/mcp/oauth/authorize',
        '/gmail/mcp/oauth/token',
        '/gmail/mcp/oauth/register',
    ],
)
def test_middleware_rejects_public_mcp_path_collision(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    mcp_path: str,
) -> None:
    mismatched = replace(service_config, mcp_path=mcp_path)

    with pytest.raises(ValueError, match='MCP path'):
        BearerAuthMiddleware(
            Starlette(), config=mismatched, oauth_state=oauth_state
        )


@pytest.mark.parametrize(
    ('mcp_path', 'method', 'expected_status'),
    [
        ('/', 'GET', 401),
        ('/', 'HEAD', 401),
        ('/', 'POST', 401),
        ('/gmail/mcp', 'GET', 200),
        ('/gmail/mcp', 'HEAD', 200),
        ('/gmail/mcp', 'POST', 401),
    ],
)
def test_root_exemption_respects_mcp_path_and_method(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    mcp_path: str,
    method: str,
    expected_status: int,
) -> None:
    config = replace(service_config, mcp_path=mcp_path)

    async def root(_: Request) -> PlainTextResponse:
        """Return root endpoint response."""
        return PlainTextResponse('root')

    app = Starlette(routes=[Route('/', root, methods=['GET', 'HEAD', 'POST'])])
    app.add_middleware(
        BearerAuthMiddleware,
        config=config,
        oauth_state=oauth_state,
    )
    with TestClient(app) as test_client:
        response = test_client.request(method, '/')

    assert response.status_code == expected_status


def test_missing_auth_returns_rfc9728_challenge(
    client: TestClient,
) -> None:
    response = client.get('/gmail/mcp')

    assert response.status_code == 401
    assert not response.content
    challenge = response.headers['WWW-Authenticate']
    assert challenge.startswith('Bearer ')
    assert f'resource_metadata="{RESOURCE_METADATA}"' in challenge
    assert 'scope="gmail.read"' in challenge
    assert 'error=' not in challenge


def test_unsupported_authentication_scheme_returns_plain_challenge(
    client: TestClient,
) -> None:
    response = client.get(
        '/gmail/mcp', headers={'Authorization': 'Basic value'}
    )

    assert response.status_code == 401
    assert not response.content
    challenge = response.headers['WWW-Authenticate']
    assert 'scope="gmail.read"' in challenge
    assert 'error=' not in challenge


@pytest.mark.parametrize(
    'authorization',
    [
        'Bearer',
        'Bearer ',
        'Bearer\tv1.unknown.secret',
        'Bearer v1.unknown.secret!',
    ],
)
def test_malformed_bearer_returns_invalid_request(
    client: TestClient,
    authorization: str,
) -> None:
    response = client.get(
        '/gmail/mcp', headers={'Authorization': authorization}
    )

    assert response.status_code == 400
    challenge = response.headers['WWW-Authenticate']
    assert 'scope="gmail.read"' in challenge
    assert 'error="invalid_request"' in challenge


def test_unknown_oauth_token_returns_invalid_token(
    client: TestClient,
) -> None:
    response = client.get(
        '/gmail/mcp',
        headers={'Authorization': 'Bearer v1.unknown.secret'},
    )

    assert response.status_code == 401
    assert response.json() == {'error': 'Invalid token'}
    assert 'error="invalid_token"' in response.headers['WWW-Authenticate']


@pytest.mark.parametrize(
    ('policy', 'capabilities', 'path', 'expected_status'),
    [
        (LEGACY_FULL, (), '/admin', 200),
        (LEGACY_FULL, ('gmail.read',), '/admin', 401),
        (MCP_READONLY_V1, READONLY_CAPABILITIES, '/gmail/mcp', 200),
        (MCP_READONLY_V1, (), '/gmail/mcp', 401),
        (
            MCP_READONLY_V1,
            ('gmail.read', 'gmail.write'),
            '/gmail/mcp',
            401,
        ),
        ('unknown-policy', (), '/gmail/mcp', 401),
    ],
)
def test_bearer_enforces_policy_capability_matrix(
    client: TestClient,
    oauth_state: OAuthState,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
    capabilities: tuple[str, ...],
    path: str,
    expected_status: int,
) -> None:
    metadata = TokenMetadata(
        token_id='fixed-token',
        client_id='fixed-client',
        policy=policy,
        capabilities=capabilities,
        resource=RESOURCE,
        issued_at=1.0,
        expires_at=2.0,
        revoked_at=None,
    )
    monkeypatch.setattr(
        oauth_state,
        'lookup_access_token',
        lambda _: metadata,
    )

    response = client.get(
        path,
        headers={'Authorization': 'Bearer v1.FixedToken.MixedSecret'},
    )

    assert response.status_code == expected_status


def test_duplicate_authorization_fields_return_invalid_request(
    client: TestClient,
    oauth_state: OAuthState,
) -> None:
    token, _ = _issue_token(oauth_state)

    response = client.get(
        '/gmail/mcp',
        headers=[
            ('Authorization', f'Bearer {token}'),
            ('Authorization', f'Bearer {token}'),
        ],
    )

    assert response.status_code == 400
    assert 'error="invalid_request"' in response.headers['WWW-Authenticate']


def test_valid_oauth_token_passes_through(
    client: TestClient,
    oauth_state: OAuthState,
) -> None:
    token, client_id = _issue_token(oauth_state)

    response = client.get(
        '/gmail/mcp', headers={'Authorization': f'Bearer {token}'}
    )

    assert response.status_code == 200
    assert response.text == f'oauth:{client_id}'
    assert 'WWW-Authenticate' not in response.headers
    assert current_request_context() is None


@pytest.mark.parametrize('scheme', ['bearer', 'BEARER', 'bEaReR'])
def test_bearer_scheme_is_case_insensitive(
    client: TestClient,
    oauth_state: OAuthState,
    scheme: str,
) -> None:
    token, client_id = _issue_token(oauth_state)

    response = client.get(
        '/gmail/mcp', headers={'Authorization': f'{scheme} {token}'}
    )

    assert response.status_code == 200
    assert response.text == f'oauth:{client_id}'


def test_bearer_allows_multiple_ascii_separator_spaces(
    client: TestClient,
    oauth_state: OAuthState,
) -> None:
    token, client_id = _issue_token(oauth_state)

    response = client.get(
        '/gmail/mcp', headers={'Authorization': f'Bearer   {token}'}
    )

    assert response.status_code == 200
    assert response.text == f'oauth:{client_id}'


def test_readonly_token_is_limited_to_mcp_path(
    client: TestClient,
    oauth_state: OAuthState,
) -> None:
    token, _ = _issue_token(oauth_state)

    response = client.get(
        '/admin', headers={'Authorization': f'Bearer {token}'}
    )

    assert response.status_code == 403
    assert response.json() == {'error': 'Insufficient scope'}
    assert 'error="insufficient_scope"' in response.headers['WWW-Authenticate']
    assert 'scope="gmail.read"' in response.headers['WWW-Authenticate']


def test_token_for_foreign_resource_is_rejected(
    client: TestClient,
    oauth_state: OAuthState,
) -> None:
    token, _ = _issue_token(oauth_state, FOREIGN_RESOURCE)

    response = client.get(
        '/gmail/mcp', headers={'Authorization': f'Bearer {token}'}
    )

    assert response.status_code == 401
    assert response.json() == {'error': 'Invalid token'}


def test_master_token_environment_has_no_authentication_path(
    state_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_token = 'v1.MasterToken.MixedCaseSecret'
    monkeypatch.setenv('GMAIL_MCP_TOKEN', master_token)
    config = _service_config(state_dir)
    state = OAuthState(
        config.oauth_state_path,
        download_path=tmp_path / 'downloads',
        service_id=config.service_id,
        resource=config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
    )
    try:
        with TestClient(_protected_app(config, state)) as test_client:
            response = test_client.get(
                config.mcp_path,
                headers={'Authorization': f'Bearer {master_token}'},
            )
    finally:
        state.close()

    assert response.status_code == 401
    assert response.json() == {'error': 'Invalid token'}


@pytest.mark.asyncio
async def test_token_lookup_does_not_block_event_loop(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _ = _issue_token(oauth_state)
    # Slow token lookup
    original_lookup = oauth_state.lookup_access_token

    def slow_lookup(value: str):
        """Simulate slow token lookup."""
        time.sleep(0.35)
        return original_lookup(value)

    monkeypatch.setattr(oauth_state, 'lookup_access_token', slow_lookup)

    async def endpoint(_: Request) -> PlainTextResponse:
        """Return test endpoint response."""
        return PlainTextResponse('ok')

    app = Starlette(
        routes=[
            Route(service_config.mcp_path, endpoint),
            Route('/health', endpoint),
        ]
    )
    app.add_middleware(
        BearerAuthMiddleware,
        config=service_config,
        oauth_state=oauth_state,
    )
    # Concurrent health probe
    started = asyncio.get_running_loop().time()
    protected = asyncio.create_task(
        _asgi_request(
            app,
            service_config.mcp_path,
            authorization=f'Bearer {token}',
        )
    )
    await asyncio.sleep(0.05)
    wake_delay = asyncio.get_running_loop().time() - started
    health_status, _ = await _asgi_request(app, '/health')
    protected_status, _ = await protected

    # Timing assertions
    assert protected_status == 200
    assert health_status == 200
    assert wake_delay < 0.2


@pytest.mark.asyncio
async def test_request_context_resets_in_request_task_after_success(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    token, _ = _issue_token(oauth_state)

    async def endpoint(_: Request) -> PlainTextResponse:
        """Return contextual test response."""
        assert current_request_context() is not None
        return PlainTextResponse('ok')

    app = _single_route_app(service_config, oauth_state, endpoint)
    status, _ = await _asgi_request(
        app,
        service_config.mcp_path,
        authorization=f'Bearer {token}',
    )

    assert status == 200
    assert current_request_context() is None


@pytest.mark.asyncio
async def test_request_context_resets_after_downstream_exception(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    token, _ = _issue_token(oauth_state)

    async def endpoint(_: Request) -> PlainTextResponse:
        """Raise downstream test failure."""
        assert current_request_context() is not None
        raise RuntimeError('downstream failed')

    app = _single_route_app(service_config, oauth_state, endpoint)
    with pytest.raises(RuntimeError, match='downstream failed'):
        await _asgi_request(
            app,
            service_config.mcp_path,
            authorization=f'Bearer {token}',
        )

    assert current_request_context() is None


@pytest.mark.asyncio
async def test_request_context_resets_after_downstream_cancellation(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    token, _ = _issue_token(oauth_state)
    scope: Scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'https',
        'path': service_config.mcp_path,
        'raw_path': service_config.mcp_path.encode('ascii'),
        'query_string': b'',
        'root_path': '',
        'headers': [(b'authorization', f'Bearer {token}'.encode('ascii'))],
        'client': ('127.0.0.1', 50000),
        'server': ('mcp.example.test', 443),
        'state': {},
    }
    middleware = BearerAuthMiddleware(
        Starlette(),
        config=service_config,
        oauth_state=oauth_state,
    )

    async def endpoint(_: Request) -> Response:
        """Raise downstream cancellation signal."""
        assert current_request_context() is not None
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware.dispatch(Request(scope), endpoint)

    assert current_request_context() is None


@pytest.mark.asyncio
async def test_request_context_restores_nested_context(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    token, client_id = _issue_token(oauth_state)
    outer_principal = AuthenticatedPrincipal(
        principal_id='outer',
        credential_id='outer-digest',
        client_id=None,
        policy='outer',
        capabilities=frozenset(),
        full_access=True,
    )
    outer_token = set_request_context(outer_principal, 'outer-request')

    async def endpoint(_: Request) -> PlainTextResponse:
        """Return authenticated principal identity."""
        principal = current_principal()
        assert principal is not None
        return PlainTextResponse(principal.principal_id)

    try:
        app = _single_route_app(service_config, oauth_state, endpoint)
        status, body = await _asgi_request(
            app,
            service_config.mcp_path,
            authorization=f'Bearer {token}',
        )

        restored = current_request_context()
        assert status == 200
        assert body == f'oauth:{client_id}'.encode()
        assert restored is not None
        assert restored.principal == outer_principal
        assert restored.request_id == 'outer-request'
    finally:
        reset_request_context(outer_token)


@pytest.mark.asyncio
async def test_concurrent_requests_have_isolated_contexts(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    first_token, first_client = _issue_token(oauth_state)
    second_token, second_client = _issue_token(oauth_state)
    contexts = []
    both_entered = asyncio.Event()

    async def endpoint(_: Request) -> PlainTextResponse:
        """Capture request context state."""
        context = current_request_context()
        assert context is not None
        contexts.append(context)
        if len(contexts) == 2:
            both_entered.set()
        await both_entered.wait()
        return PlainTextResponse(context.request_id)

    app = _single_route_app(service_config, oauth_state, endpoint)
    first, second = await asyncio.gather(
        _asgi_request(
            app,
            service_config.mcp_path,
            authorization=f'Bearer {first_token}',
        ),
        _asgi_request(
            app,
            service_config.mcp_path,
            authorization=f'Bearer {second_token}',
        ),
    )

    assert {first[0], second[0]} == {200}
    assert len({first[1], second[1]}) == 2
    assert {context.principal.client_id for context in contexts} == {
        first_client,
        second_client,
    }
    assert current_request_context() is None
