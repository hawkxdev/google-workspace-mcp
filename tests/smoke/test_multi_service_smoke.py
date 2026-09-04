"""Multi service smoke tests."""

import sqlite3
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import TestClient

from google_workspace_mcp.auth.state import LEGACY_FULL
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services.calendar.factory import create_calendar_app
from google_workspace_mcp.services.docs.factory import create_docs_app
from google_workspace_mcp.services.drive.factory import create_drive_app
from google_workspace_mcp.services.gmail.factory import create_gmail_app
from google_workspace_mcp.services.sheets.factory import create_sheets_app
from google_workspace_mcp.transport import create_service_app
from google_workspace_mcp.transport.extensions import Extension

SERVICES_SPEC = (
    ('GMAIL', '8431'),
    ('CALENDAR', '8432'),
    ('DRIVE', '8433'),
    ('SHEETS', '8434'),
    ('DOCS', '8435'),
)


def _setup_srv(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    service: str,
    port: str,
) -> None:
    """Setup isolated service environment."""
    d = root / service.lower()
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    dl = d / 'dl'
    dl.mkdir(parents=True, mode=0o700, exist_ok=True)
    for k, v in {
        f'{service}_MCP_PORT': port,
        f'{service}_MCP_PUBLIC_URL': (
            f'https://mcp.example.test/{service.lower()}'
        ),
        f'{service}_MCP_PATH': f'/{service.lower()}/mcp',
        f'{service}_OAUTH_STATE_PATH': str(d / 'oauth.db'),
        f'{service}_GOOGLE_TOKEN_PATH': str(d / 'token.json'),
        f'{service}_AUDIT_LOG_PATH': str(d / 'audit.jsonl'),
        f'{service}_MCP_DOWNLOAD_PATH': str(dl),
        f'{service}_OAUTH_LOGIN_USERNAME': 'smoke-user',
        f'{service}_OAUTH_LOGIN_PASSWORD': 'smoke-password',
    }.items():
        monkeypatch.setenv(k, v)


class _CollidingRouteExtension(Extension):
    """Model test extension."""

    def __init__(self, path: str) -> None:
        """Initialize test double."""
        self._path = path

    def register_routes(self, app: object) -> None:
        """Register colliding extension route."""

        async def _endpoint(_request: Request) -> JSONResponse:
            """Provide test endpoint."""
            return JSONResponse({'collision': True})

        routes = getattr(app, 'routes', None)
        if isinstance(routes, list):
            routes.append(
                Route(self._path, _endpoint, methods=['GET', 'POST'])
            )


class _CustomRouteExtension(Extension):
    """Model test extension."""

    def __init__(self, path: str, tag: str) -> None:
        """Initialize test double."""
        self._path = path
        self._tag = tag

    def register_routes(self, app: object) -> None:
        """Register custom extension route."""

        async def _endpoint(_request: Request) -> JSONResponse:
            """Provide test endpoint."""
            return JSONResponse({'tag': self._tag})

        routes = getattr(app, 'routes', None)
        if isinstance(routes, list):
            routes.append(Route(self._path, _endpoint, methods=['GET']))


class _MountExtension(Extension):
    """Model test extension."""

    def register_routes(self, app: object) -> None:
        """Register mount extension route."""
        routes = getattr(app, 'routes', None)
        if isinstance(routes, list):
            routes.append(Mount('/ext_mount', app=JSONResponse({})))


class _WebSocketExtension(Extension):
    """Model test extension."""

    def register_routes(self, app: object) -> None:
        """Register websocket extension route."""

        async def _ws_endpoint(_ws: object) -> None:
            """Provide websocket endpoint."""
            pass

        routes = getattr(app, 'routes', None)
        if isinstance(routes, list):
            routes.append(WebSocketRoute('/ext_ws', _ws_endpoint))


def test_five_service_identity_uniqueness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for srv, port in SERVICES_SPEC:
        _setup_srv(monkeypatch, tmp_path / 'id_root', srv, port)

    res_gmail = create_gmail_app()
    res_cal = create_calendar_app()
    res_drv = create_drive_app()
    res_sht = create_sheets_app()
    res_doc = create_docs_app()

    all_results = [res_gmail, res_cal, res_drv, res_sht, res_doc]
    all_states = [r[2] for r in all_results]

    try:
        configs = [ServiceConfig.from_env(s.lower()) for s, _ in SERVICES_SPEC]
        issuers = [c.public_url for c in configs]
        resources = [c.resource_url for c in configs]
        mcp_paths = [c.mcp_path for c in configs]
        ports = [c.port for c in configs]
        state_paths = [st.path for st in all_states]
        download_paths = [st.download_path for st in all_states]

        assert len(set(issuers)) == 5
        assert len(set(resources)) == 5
        assert len(set(mcp_paths)) == 5
        assert len(set(ports)) == 5
        assert len(set(state_paths)) == 5
        assert len(set(download_paths)) == 5

        for c in configs:
            svc = c.service_id
            assert c.public_url == f'https://mcp.example.test/{svc}'
            assert c.resource_url == f'https://mcp.example.test/{svc}/mcp'
            assert c.mcp_path == f'/{svc}/mcp'
    finally:
        for st in all_states:
            st.close()


def test_cross_service_and_old_audience_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for srv, port in SERVICES_SPEC:
        _setup_srv(monkeypatch, tmp_path / 'cross_root', srv, port)

    res_gmail = create_gmail_app()
    res_cal = create_calendar_app()
    res_drv = create_drive_app()
    res_sht = create_sheets_app()
    res_doc = create_docs_app()

    all_results = [res_gmail, res_cal, res_drv, res_sht, res_doc]
    all_apps = [r[0] for r in all_results]
    all_states = [r[2] for r in all_results]

    try:
        for (srv_name, _), app, state in zip(
            SERVICES_SPEC, all_apps, all_states, strict=True
        ):
            svc_lower = srv_name.lower()
            client = TestClient(app, raise_server_exceptions=False)
            callback_uri = f'https://client.example.test/{svc_lower}/cb'
            reg = state.register_client((callback_uri,))
            client_id = reg.client.client_id
            valid_resource = f'https://mcp.example.test/{svc_lower}/mcp'
            old_issuer_resource = f'https://mcp.example.test/{svc_lower}'

            issued = state.issue_access_token(
                client_id=client_id,
                resource=valid_resource,
            )
            res_valid = client.post(
                f'/{svc_lower}/mcp',
                headers={'Authorization': f'Bearer {issued.access_token}'},
            )
            assert res_valid.status_code != 401

            old_issued = state.issue_access_token(
                client_id=client_id,
                resource=valid_resource,
            )
            with sqlite3.connect(state.path) as conn:
                conn.execute(
                    'UPDATE access_tokens SET resource = ? WHERE token_id = ?',
                    (old_issuer_resource, old_issued.token.token_id),
                )
            res_old = client.post(
                f'/{svc_lower}/mcp',
                headers={'Authorization': f'Bearer {old_issued.access_token}'},
            )
            assert res_old.status_code == 401
            assert 'invalid_token' in res_old.headers.get(
                'WWW-Authenticate', ''
            )

            for (other_name, _), other_app in zip(
                SERVICES_SPEC, all_apps, strict=True
            ):
                if other_name == srv_name:
                    continue
                other_lower = other_name.lower()
                other_client = TestClient(
                    other_app, raise_server_exceptions=False
                )

                res_cross = other_client.post(
                    f'/{other_lower}/mcp',
                    headers={'Authorization': f'Bearer {issued.access_token}'},
                )
                assert res_cross.status_code == 401
                assert 'invalid_token' in res_cross.headers.get(
                    'WWW-Authenticate', ''
                )

                res_bad_target = client.post(
                    f'/{svc_lower}/oauth/token',
                    data={
                        'grant_type': 'refresh_token',
                        'refresh_token': 'dummy-token',
                        'client_id': client_id,
                        'resource': (
                            f'https://mcp.example.test/{other_lower}/mcp'
                        ),
                    },
                )
                assert res_bad_target.status_code == 400
                assert res_bad_target.json().get('error') in {
                    'invalid_target',
                    'invalid_request',
                    'invalid_grant',
                }
    finally:
        for st in all_states:
            st.close()


@pytest.mark.parametrize('method', ['get', 'head', 'post'])
@pytest.mark.parametrize(
    'path',
    [
        '/register',
        '/authorize',
        '/token',
        '/.well-known/oauth-protected-resource',
        '/.well-known/oauth-authorization-server',
    ],
)
def test_root_oauth_aliases_stay_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method: str,
    path: str,
) -> None:
    _setup_srv(monkeypatch, tmp_path, 'GMAIL', '8431')
    app, _, state = create_gmail_app()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code in {401, 404}
        assert 'location' not in response.headers
    finally:
        state.close()


def test_cross_service_token_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uri = 'https://client.example.test/cb'
    g_res = 'https://mcp.example.test/gmail/mcp'
    d_res = 'https://mcp.example.test/drive/mcp'
    for s, p in (('GMAIL', '8431'), ('DRIVE', '8433')):
        _setup_srv(monkeypatch, tmp_path, s, p)

    app_gmail, _, state_gmail = create_gmail_app()
    app_drive, _, state_drive = create_drive_app()
    try:
        registered = state_drive.register_client((uri,))
        issued = state_drive.issue_access_token(
            client_id=registered.client.client_id, resource=d_res
        )
        with sqlite3.connect(state_drive.path) as conn:
            conn.execute(
                'UPDATE access_tokens SET resource = ? WHERE token_id = ?',
                (g_res, issued.token.token_id),
            )
        meta = state_drive.lookup_access_token(issued.access_token)
        assert meta is not None and meta.resource == g_res

        c_drive = TestClient(app_drive, raise_server_exceptions=False)
        res = c_drive.post(
            '/drive/mcp',
            headers={'Authorization': f'Bearer {issued.access_token}'},
        )
        assert res.status_code == 401
        assert 'invalid_token' in res.headers.get('WWW-Authenticate', '')

        # Step: File and state isolation assertions
        assert state_drive.path != state_gmail.path
        assert state_drive.download_path != state_gmail.download_path
        assert state_gmail.lookup_access_token(issued.access_token) is None
        assert state_gmail.get_client(registered.client.client_id) is None

        cfg_gmail = ServiceConfig.from_env('gmail')
        cfg_drive = ServiceConfig.from_env('drive')
        assert cfg_gmail.oauth_state_path != cfg_drive.oauth_state_path
        assert cfg_gmail.google_token_path != cfg_drive.google_token_path
        assert cfg_gmail.audit_log_path != cfg_drive.audit_log_path
        assert cfg_gmail.download_path != cfg_drive.download_path
        assert cfg_gmail.public_url != cfg_drive.public_url
        assert cfg_gmail.resource_url != cfg_drive.resource_url
        assert cfg_gmail.port != cfg_drive.port
    finally:
        state_drive.close()
        state_gmail.close()


def test_all_five_services_multi_isolation_and_route_collisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for srv, port in SERVICES_SPEC:
        _setup_srv(monkeypatch, tmp_path / 'srv_root', srv, port)

    res_gmail = create_gmail_app()
    res_cal = create_calendar_app()
    res_drv = create_drive_app()
    res_sht = create_sheets_app()
    res_doc = create_docs_app()

    all_results = [res_gmail, res_cal, res_drv, res_sht, res_doc]
    all_apps = [r[0] for r in all_results]
    all_servers = [r[1] for r in all_results]
    all_states = [r[2] for r in all_results]

    try:
        assert len({id(a) for a in all_apps}) == 5
        assert len({id(s) for s in all_servers}) == 5
        assert len({id(st) for st in all_states}) == 5

        # Step: Distinct metadata and persistence paths across 5 services
        ports = [p for _, p in SERVICES_SPEC]
        resources = [st.resource for st in all_states]
        state_paths = [st.path for st in all_states]
        download_paths = [st.download_path for st in all_states]
        assert len(set(ports)) == 5
        assert len(set(resources)) == 5
        assert len(set(state_paths)) == 5
        assert len(set(download_paths)) == 5

        # Step: Verify tool inventory per service
        # Step: Gmail 18, Calendar 9, Drive 10, Sheets 11, Docs 7
        assert len(res_gmail[1]._tool_manager.list_tools()) == 18
        assert len(res_cal[1]._tool_manager.list_tools()) == 9
        assert len(res_drv[1]._tool_manager.list_tools()) == 10
        assert len(res_sht[1]._tool_manager.list_tools()) == 11
        assert len(res_doc[1]._tool_manager.list_tools()) == 7

        assert len(res_gmail[2].readonly_capabilities) == 7
        assert len(res_cal[2].readonly_capabilities) == 5
        assert len(res_drv[2].readonly_capabilities) == 3
        assert len(res_sht[2].readonly_capabilities) == 3
        assert len(res_doc[2].readonly_capabilities) == 2

        # Step: Check endpoints and route collisions on all 5 services
        for (srv_name, _), app, state in zip(
            SERVICES_SPEC, all_apps, all_states, strict=True
        ):
            srv_lower = srv_name.lower()
            client = TestClient(app, raise_server_exceptions=False)

            # Step: Public /health check
            res_health = client.get('/health')
            assert res_health.status_code == 200
            assert res_health.json() == {
                'service': srv_lower,
                'status': 'ok',
            }
            health_body = res_health.text
            assert 'paths' not in health_body
            assert 'audit' not in health_body
            assert 'secret' not in health_body

            # Step: Protected /ready check without auth (401)
            res_ready_unauth = client.get('/ready')
            assert res_ready_unauth.status_code == 401
            assert 'Bearer' in res_ready_unauth.headers.get(
                'WWW-Authenticate', ''
            )

            # Step: Issue valid token for this service
            reg = state.register_client(
                (f'https://client.example.test/{srv_lower}/callback',)
            )
            tok = state.issue_access_token(
                client_id=reg.client.client_id,
                resource=f'https://mcp.example.test/{srv_lower}/mcp',
            )

            # Step: Protected /ready check with valid auth (200)
            res_ready_auth = client.get(
                '/ready',
                headers={'Authorization': f'Bearer {tok.access_token}'},
            )
            assert res_ready_auth.status_code == 200
            assert res_ready_auth.json() == {'status': 'ready'}

            # Step: OAuth metadata checks
            res_res_meta = client.get(
                f'/.well-known/oauth-protected-resource/{srv_lower}/mcp'
            )
            assert res_res_meta.status_code == 200
            assert (
                res_res_meta.json().get('resource')
                == f'https://mcp.example.test/{srv_lower}/mcp'
            )
            assert res_res_meta.json().get('authorization_servers') == [
                f'https://mcp.example.test/{srv_lower}'
            ]

            res_as_meta = client.get(
                f'/.well-known/oauth-authorization-server/{srv_lower}'
            )
            assert res_as_meta.status_code == 200
            assert (
                res_as_meta.json().get('issuer')
                == f'https://mcp.example.test/{srv_lower}'
            )
            assert (
                res_as_meta.json().get('resource')
                == f'https://mcp.example.test/{srv_lower}/mcp'
            )

            # Step: MCP route unauthenticated check (401)
            res_mcp_unauth = client.post(f'/{srv_lower}/mcp')
            assert res_mcp_unauth.status_code == 401

            # Step: Cross service token rejection
            for other_srv, other_app in zip(
                SERVICES_SPEC, all_apps, strict=True
            ):
                if other_srv[0] == srv_name:
                    continue
                other_client = TestClient(
                    other_app, raise_server_exceptions=False
                )
                res_cross = other_client.post(
                    f'/{other_srv[0].lower()}/mcp',
                    headers={'Authorization': f'Bearer {tok.access_token}'},
                )
                assert res_cross.status_code == 401
                assert 'invalid_token' in res_cross.headers.get(
                    'WWW-Authenticate', ''
                )
    finally:
        for st in all_states:
            st.close()


def test_extension_route_collisions_and_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_srv(monkeypatch, tmp_path, 'GMAIL', '8431')
    cfg = ServiceConfig.from_env('gmail')

    # Step: Colliding route extensions must be rejected on create_service_app
    for bad_path in (
        '/health',
        '/.well-known/oauth-protected-resource/gmail/mcp',
        '/.well-known/oauth-authorization-server/gmail',
        '/gmail/oauth/register',
        '/gmail/oauth/token',
        '/gmail/oauth/authorize',
        '/gmail/mcp',
    ):
        ext = _CollidingRouteExtension(bad_path)
        with pytest.raises(ValueError):
            create_service_app(cfg, extensions=[ext])

    # Step: Unsafe extension constructs are rejected
    with pytest.raises(ValueError, match='Mount is not allowed'):
        create_service_app(cfg, extensions=[_MountExtension()])

    with pytest.raises(ValueError, match='WebSocketRoute is not allowed'):
        create_service_app(cfg, extensions=[_WebSocketExtension()])

    # Step: Independent extension routes
    ext1 = _CustomRouteExtension('/custom/probe1', 'tag1')
    app, server, state = create_service_app(cfg, extensions=[ext1])
    try:
        client = TestClient(app, raise_server_exceptions=False)
        res_unauth = client.get('/custom/probe1')
        assert res_unauth.status_code == 401

        # Step: Standard readonly token has insufficient scope for custom route
        reg_std = state.register_client(('https://client.example.test/cb',))
        tok_std = state.issue_access_token(
            client_id=reg_std.client.client_id,
            resource='https://mcp.example.test/gmail/mcp',
        )
        res_std = client.get(
            '/custom/probe1',
            headers={'Authorization': f'Bearer {tok_std.access_token}'},
        )
        assert res_std.status_code == 403
        assert 'insufficient_scope' in res_std.headers.get(
            'WWW-Authenticate', ''
        )

        # Step: Full access token can access custom route
        reg_full = state.ensure_static_client(
            client_id='smoke-admin',
            redirect_uris=('https://client.example.test/cb',),
            policy=LEGACY_FULL,
            capabilities=(),
        )
        tok_full = state.issue_access_token(
            client_id=reg_full.client_id,
            resource='https://mcp.example.test/gmail/mcp',
        )
        res_full = client.get(
            '/custom/probe1',
            headers={'Authorization': f'Bearer {tok_full.access_token}'},
        )
        assert res_full.status_code == 200
        assert res_full.json() == {'tag': 'tag1'}
    finally:
        state.close()
