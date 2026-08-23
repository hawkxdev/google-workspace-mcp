import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import (
    ServiceConfig,
    _validate_mcp_path,
)
from google_workspace_mcp.transport.authorization import PolicyMCPServer
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.server import build_app

RESOURCE = 'https://mcp.example.test/gmail/mcp'
LOGIN_USER = 'admin'
LOGIN_PASS = 'secret-password'


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    state_dir = tmp_path / 'state'
    state_dir.mkdir(mode=0o700, parents=True)
    return ServiceConfig(
        service_id='gmail',
        public_url=RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=state_dir / 'downloads',
        oauth_state_path=state_dir / 'oauth_state.sqlite3',
        google_token_path=state_dir / 'google_token.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username=LOGIN_USER,
        oauth_login_password=LOGIN_PASS,
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )


@pytest.fixture
def oauth_state(
    service_config: ServiceConfig,
    tmp_path: Path,
) -> Iterator[OAuthState]:
    download_dir = tmp_path / 'downloads'
    download_dir.mkdir(mode=0o700, parents=True)
    with OAuthState(
        service_config.oauth_state_path,
        service_id='gmail',
        resource=RESOURCE,
        download_path=download_dir,
    ) as state:
        yield state


def test_routes_and_forwarded_headers(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    server = PolicyMCPServer('gmail')
    app = build_app(service_config, oauth_state, server)
    client = TestClient(app, raise_server_exceptions=False)

    res_h = client.get(
        '/health', headers={'X-Forwarded-For': '127.0.0.1, 10.0.0.1'}
    )
    assert res_h.status_code == 200
    assert res_h.json() == {
        'service': service_config.service_id,
        'status': 'ok',
    }
    assert 'paths' not in res_h.json() and 'audit' not in res_h.json()
    assert client.get('/ready').status_code == 401
    assert client.get(service_config.mcp_path).status_code in {401, 405, 400}


def test_assembled_app_default_root_requires_auth(
    service_config: ServiceConfig,
    tmp_path: Path,
) -> None:
    root_resource = 'https://mcp.example.test'
    state_dir = tmp_path / 'root_state'
    state_dir.mkdir(mode=0o700, parents=True)
    root_cfg = ServiceConfig(
        service_id='gmail',
        public_url=root_resource,
        mcp_path='/',
        host='127.0.0.1',
        port=8431,
        download_path=state_dir / 'downloads',
        oauth_state_path=state_dir / 'oauth_root.sqlite3',
        google_token_path=state_dir / 'google_token.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username=LOGIN_USER,
        oauth_login_password=LOGIN_PASS,
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )
    with OAuthState(
        root_cfg.oauth_state_path,
        service_id='gmail',
        resource=root_resource,
        download_path=root_cfg.download_path,
    ) as root_state:
        server = PolicyMCPServer('gmail')
        app = build_app(root_cfg, root_state, server)
        paths = [getattr(r, 'path', None) for r in app.routes]
        assert '/' in paths

        with TestClient(app) as client:
            for method in ('get', 'head', 'post'):
                r = getattr(client, method)('/')
                assert r.status_code in {401, 400}
                assert 'MCP-Protocol-Version' not in r.headers


def test_assembled_app_offroot_probe_is_liveness_only(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    server = PolicyMCPServer('gmail')
    app = build_app(service_config, oauth_state, server)
    paths = [getattr(r, 'path', None) for r in app.routes]
    assert service_config.mcp_path in paths
    assert '/' in paths

    with TestClient(app) as client:
        r = client.get('/')
        assert r.status_code == 200
        assert r.headers.get('MCP-Protocol-Version') == '2025-06-18'
        assert not r.content

        head = client.head('/')
        assert head.status_code == 200
        assert head.headers.get('MCP-Protocol-Version') == '2025-06-18'
        assert not head.content

        assert client.post('/').status_code in {401, 400}
        assert client.get(service_config.mcp_path).status_code in {401, 400}
        assert client.post(service_config.mcp_path).status_code in {401, 400}


@pytest.mark.parametrize('value', ['/', '/mcp', '/gmail/mcp', '/a-b_c'])
def test_validate_accepts_clean_paths(value: str) -> None:
    _validate_mcp_path(value)


@pytest.mark.parametrize(
    'value',
    [
        '',
        'mcp',
        '/mcp/',
        '/a?b',
        '/a#b',
        '//a',
    ],
)
def test_validate_rejects_malformed_paths(value: str) -> None:
    with pytest.raises(ValueError):
        _validate_mcp_path(value)


@pytest.mark.parametrize(
    'value',
    [
        '/health',
        '/ready',
        '/oauth',
        '/oauth/token',
        '/oauth/authorize',
        '/.well-known',
        '/.well-known/oauth-protected-resource',
    ],
)
def test_validate_rejects_auth_exempt_and_system_collisions(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _validate_mcp_path(value)


@pytest.mark.parametrize(
    'value',
    [
        '/.',
        '/mcp/..',
        '/oauth%2ftoken',
        '/a b',
        '/a\x01b',
    ],
)
def test_validate_rejects_dot_encoding_and_control_chars(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _validate_mcp_path(value)


def test_ready_endpoint_accessible_with_bearer_token(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    registered = oauth_state.register_client(
        ('https://client.example.test/cb',)
    )
    issued = oauth_state.issue_access_token(
        client_id=registered.client.client_id,
        resource=RESOURCE,
    )
    server = PolicyMCPServer('gmail')
    app = build_app(service_config, oauth_state, server)
    client = TestClient(app)

    res = client.get(
        '/ready',
        headers={'Authorization': f'Bearer {issued.access_token}'},
    )
    assert res.status_code == 200
    assert res.json() == {'status': 'ready'}


class _DummyExtension(Extension):
    def __init__(self, state: OAuthState | None = None) -> None:
        self.state = state
        self.routes_registered = False
        self.shut_down = False
        self.state_open_at_shutdown: bool | None = None

    def register_routes(self, app: Any) -> None:
        self.routes_registered = True

        async def ping(_: Request) -> PlainTextResponse:
            return PlainTextResponse('pong')

        app.routes.append(Route('/ext/ping', ping, methods=['GET']))

    def shutdown(self) -> None:
        self.shut_down = True
        if self.state is not None:
            self.state_open_at_shutdown = not self.state._closed


def test_extension_route_and_lifespan_shutdown(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = oauth_state.register_client(
        ('https://client.example.test/cb',)
    )
    readonly_token = oauth_state.issue_access_token(
        client_id=registered.client.client_id,
        resource=RESOURCE,
    )
    ext = _DummyExtension(oauth_state)
    server = PolicyMCPServer('gmail')
    app = build_app(service_config, oauth_state, server, extensions=[ext])

    assert ext.routes_registered is True
    assert oauth_state._closed is False
    with TestClient(app) as client:
        assert client.get('/ext/ping').status_code == 401
        res_readonly = client.get(
            '/ext/ping',
            headers={'Authorization': f'Bearer {readonly_token.access_token}'},
        )
        assert res_readonly.status_code == 403

        from google_workspace_mcp.auth.state import LEGACY_FULL, TokenMetadata

        metadata = TokenMetadata(
            token_id='full-token',
            client_id='full-client',
            policy=LEGACY_FULL,
            capabilities=(),
            resource=RESOURCE,
            issued_at=1.0,
            expires_at=2.0,
            revoked_at=None,
        )
        oauth_state.lookup_access_token = lambda _: metadata
        res_full = client.get(
            '/ext/ping',
            headers={'Authorization': 'Bearer v1.FullToken.MixedSecret'},
        )
        assert res_full.status_code == 200
        assert res_full.text == 'pong'
    assert ext.shut_down is True
    assert ext.state_open_at_shutdown is True
    assert oauth_state._closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        oauth_state.table_names()
    # Double close is safe
    oauth_state.close()
    assert oauth_state._closed is True


class _UnsafeMountExtension(Extension):
    def register_routes(self, app: Any) -> None:
        app.routes.append(Mount('/ext', app=JSONResponse({'ext': 'mount'})))


def test_extension_rejects_unsafe_mount(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    ext = _UnsafeMountExtension()
    server = PolicyMCPServer('gmail')
    with pytest.raises(ValueError, match='extension Mount is not allowed'):
        build_app(service_config, oauth_state, server, extensions=[ext])


class _UnsafeWebSocketExtension(Extension):
    def register_routes(self, app: Any) -> None:
        async def ws_endpoint(_: Any) -> None:
            pass

        app.routes.append(WebSocketRoute('/ws', ws_endpoint))


def test_extension_rejects_unsafe_websocket(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    ext = _UnsafeWebSocketExtension()
    server = PolicyMCPServer('gmail')
    with pytest.raises(
        ValueError, match='extension WebSocketRoute is not allowed'
    ):
        build_app(service_config, oauth_state, server, extensions=[ext])


class _CollidingRouteExtension(Extension):
    def register_routes(self, app: Any) -> None:
        async def mock_health(_: Request) -> JSONResponse:
            return JSONResponse({'evil': True})

        app.routes.append(Route('/health', mock_health, methods=['GET']))


def test_extension_rejects_auth_exempt_collision(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    ext = _CollidingRouteExtension()
    server = PolicyMCPServer('gmail')
    with pytest.raises(
        ValueError, match='extension route.*covers auth-exempt path'
    ):
        build_app(service_config, oauth_state, server, extensions=[ext])


class _McpPathCollidingExtension(Extension):
    def __init__(self, mcp_path: str) -> None:
        self._mcp_path = mcp_path

    def register_routes(self, app: Any) -> None:
        async def fake_mcp(_: Request) -> PlainTextResponse:
            return PlainTextResponse('fake')

        app.routes.append(Route(self._mcp_path, fake_mcp, methods=['POST']))


def test_extension_rejects_mcp_transport_collision(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    ext = _McpPathCollidingExtension(service_config.mcp_path)
    server = PolicyMCPServer('gmail')
    with pytest.raises(
        ValueError, match='extension route.*covers MCP transport path'
    ):
        build_app(service_config, oauth_state, server, extensions=[ext])


def test_forwarded_headers_proxy_trusted_vs_untrusted(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    server = PolicyMCPServer('gmail')
    app = build_app(service_config, oauth_state, server)
    wrapped_app = ProxyHeadersMiddleware(
        app,
        trusted_hosts=list(service_config.forwarded_allow_ips),
    )

    trusted_client = TestClient(wrapped_app, client=('127.0.0.1', 54321))
    res_trusted = trusted_client.get(
        '/health',
        headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'proxy.example.test',
        },
    )
    assert res_trusted.status_code == 200

    untrusted_client = TestClient(wrapped_app, client=('198.51.100.1', 54321))
    res_untrusted = untrusted_client.get(
        '/health',
        headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'evil.example.test',
        },
    )
    assert res_untrusted.status_code == 200


def test_bearer_collision_guards_health_and_ready(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    for bad_path in ('/health', '/ready'):
        bad_cfg = ServiceConfig(
            service_id='gmail',
            public_url=RESOURCE,
            mcp_path=bad_path,
            host='127.0.0.1',
            port=8431,
            download_path=service_config.download_path,
            oauth_state_path=service_config.oauth_state_path,
            google_token_path=service_config.google_token_path,
            audit_log_path=service_config.audit_log_path,
            oauth_login_username=LOGIN_USER,
            oauth_login_password=LOGIN_PASS,
            allowed_hosts=(),
            forwarded_allow_ips=('127.0.0.1',),
            legacy_clients_path=None,
            approved_legacy_client_ids=frozenset(),
            access_token_ttl_seconds=86400,
            refresh_token_ttl_seconds=2592000,
        )
        with pytest.raises(
            ValueError, match='MCP path collides with a public auth route'
        ):
            BearerAuthMiddleware(
                app=JSONResponse({}),
                config=bad_cfg,
                oauth_state=oauth_state,
            )
