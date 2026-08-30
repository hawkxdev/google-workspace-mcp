import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_workspace_mcp.auth.state import (
    LEGACY_FULL,
    OAuthState,
    TokenMetadata,
)
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services import (
    create_calendar_app,
    create_docs_app,
    create_drive_app,
    create_gmail_app,
    create_sheets_app,
)
from google_workspace_mcp.services.calendar.factory import (
    create_calendar_app as create_calendar_from_module,
)
from google_workspace_mcp.services.docs.factory import (
    create_docs_app as create_docs_from_module,
)
from google_workspace_mcp.services.drive.factory import (
    create_drive_app as create_drive_from_module,
)
from google_workspace_mcp.services.gmail.factory import (
    create_gmail_app as create_gmail_from_module,
)
from google_workspace_mcp.services.sheets.factory import (
    create_sheets_app as create_sheets_from_module,
)
from google_workspace_mcp.transport import create_service_app
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension


def _setup_service_env(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    service: str,
    port: str,
) -> None:
    d = root / service.lower()
    d.mkdir(parents=True, mode=0o700)
    dl = d / 'dl'
    dl.mkdir(parents=True, mode=0o700)
    for k, v in {
        f'{service}_MCP_PORT': port,
        f'{service}_MCP_PUBLIC_URL': f'https://mcp.example.test/{service.lower()}',
        f'{service}_OAUTH_STATE_PATH': str(d / 'oauth.db'),
        f'{service}_GOOGLE_TOKEN_PATH': str(d / 'token.json'),
        f'{service}_AUDIT_LOG_PATH': str(d / 'audit.jsonl'),
        f'{service}_MCP_DOWNLOAD_PATH': str(dl),
        f'{service}_OAUTH_LOGIN_USERNAME': 'admin',
        f'{service}_OAUTH_LOGIN_PASSWORD': 'secret-password',
    }.items():
        monkeypatch.setenv(k, v)


def test_all_five_factories_maintain_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    services_spec = [
        ('GMAIL', '8431'),
        ('CALENDAR', '8432'),
        ('DRIVE', '8433'),
        ('SHEETS', '8434'),
        ('DOCS', '8435'),
    ]
    for srv_name, port in services_spec:
        _setup_service_env(monkeypatch, tmp_path / 'services', srv_name, port)

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

        server_names = [s.name for s in all_servers]
        assert server_names == [
            'gmail',
            'calendar',
            'drive',
            'sheets',
            'docs',
        ]

        state_service_ids = [st.service_id for st in all_states]
        assert state_service_ids == [
            'gmail',
            'calendar',
            'drive',
            'sheets',
            'docs',
        ]

        state_paths = [st.path for st in all_states]
        assert len(set(state_paths)) == 5

        state_resources = [st.resource for st in all_states]
        assert state_resources == [
            'https://mcp.example.test/gmail/mcp',
            'https://mcp.example.test/calendar/mcp',
            'https://mcp.example.test/drive/mcp',
            'https://mcp.example.test/sheets/mcp',
            'https://mcp.example.test/docs/mcp',
        ]

        for (srv_name, _), app in zip(services_spec, all_apps, strict=True):
            client = TestClient(app, raise_server_exceptions=False)
            res = client.get('/health')
            assert res.status_code == 200
            assert res.json() == {
                'service': srv_name.lower(),
                'status': 'ok',
            }
    finally:
        for st in all_states:
            st.close()


def test_package_level_and_module_level_exports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert create_gmail_app is create_gmail_from_module
    assert create_calendar_app is create_calendar_from_module
    assert create_drive_app is create_drive_from_module
    assert create_sheets_app is create_sheets_from_module
    assert create_docs_app is create_docs_from_module


def test_create_service_app_with_extensions(tmp_path: Path) -> None:
    srv_dir = tmp_path / 'custom_srv'
    srv_dir.mkdir(parents=True, mode=0o700)
    dl_dir = srv_dir / 'dl'
    dl_dir.mkdir(parents=True, mode=0o700)

    config = ServiceConfig(
        service_id='gmail',
        public_url='https://mcp.example.test/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=dl_dir,
        oauth_state_path=srv_dir / 'oauth.db',
        google_token_path=srv_dir / 'token.json',
        audit_log_path=srv_dir / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )

    class CustomExtension(Extension):
        def __init__(self) -> None:
            self.tools_registered = False
            self.routes_registered = False
            self.shut_down = False

        def register_tools(self, registrar: ToolRegistrar) -> None:
            self.tools_registered = True

            @registrar.tool(
                name='custom_ext_tool',
                required_capability='mail.read',
                available_to_readonly=True,
            )
            def custom_ext_tool() -> str:
                return 'ext_ok'

            @registrar.tool(
                name='custom_write_tool',
                required_capability='mail.write',
            )
            def custom_write_tool() -> str:
                return 'write_ok'

        def register_routes(self, app: Any) -> None:
            self.routes_registered = True

            async def ext_endpoint(_: Request) -> JSONResponse:
                return JSONResponse({'ext': 'ok'})

            app.routes.append(
                Route('/ext/status', ext_endpoint, methods=['GET'])
            )

        def shutdown(self) -> None:
            self.shut_down = True

    ext = CustomExtension()
    app, server, state = create_service_app(config, extensions=[ext])
    try:
        assert ext.tools_registered is True
        assert ext.routes_registered is True
        assert server.required_capability('custom_ext_tool') == 'mail.read'
        assert state.readonly_capabilities == ('mail.read',)

        metadata = TokenMetadata(
            token_id='full-token',
            client_id='full-client',
            policy=LEGACY_FULL,
            capabilities=(),
            resource='https://mcp.example.test/gmail/mcp',
            issued_at=1.0,
            expires_at=9999999999.0,
            revoked_at=None,
        )

        def _lookup(_: str) -> TokenMetadata:
            return metadata

        state.lookup_access_token = _lookup  # type: ignore[method-assign]

        with TestClient(app, raise_server_exceptions=False) as client:
            res_unauth = client.get('/ext/status')
            assert res_unauth.status_code == 401

            res = client.get(
                '/ext/status',
                headers={'Authorization': 'Bearer v1.FullToken.Secret'},
            )
            assert res.status_code == 200
            assert res.json() == {'ext': 'ok'}

        assert ext.shut_down is True
    finally:
        state.close()


def test_factory_with_explicit_config_override(tmp_path: Path) -> None:
    srv_dir = tmp_path / 'override_srv'
    srv_dir.mkdir(parents=True, mode=0o700)
    dl_dir = srv_dir / 'dl'
    dl_dir.mkdir(parents=True, mode=0o700)

    config = ServiceConfig(
        service_id='gmail',
        public_url='https://custom.example.com/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.2',
        port=9999,
        download_path=dl_dir,
        oauth_state_path=srv_dir / 'oauth.db',
        google_token_path=srv_dir / 'token.json',
        audit_log_path=srv_dir / 'audit.jsonl',
        oauth_login_username='custom_admin',
        oauth_login_password='custom_password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )

    app, server, state = create_gmail_app(config=config)
    try:
        assert server.name == 'gmail'
        assert state.resource == 'https://custom.example.com/gmail/mcp'
        assert state.service_id == 'gmail'
    finally:
        state.close()


def test_factory_app_lifespan_closes_oauth_state(tmp_path: Path) -> None:
    srv_dir = tmp_path / 'lifespan_srv'
    srv_dir.mkdir(parents=True, mode=0o700)
    dl_dir = srv_dir / 'dl'
    dl_dir.mkdir(parents=True, mode=0o700)

    config = ServiceConfig(
        service_id='gmail',
        public_url='https://mcp.example.test/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=dl_dir,
        oauth_state_path=srv_dir / 'oauth.db',
        google_token_path=srv_dir / 'token.json',
        audit_log_path=srv_dir / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )

    app, _server, state = create_service_app(config)
    assert state._closed is False
    assert 'clients' in state.table_names()

    with TestClient(app) as client:
        res = client.get('/health')
        assert res.status_code == 200
        assert state._closed is False

    # State unusable after lifespan
    assert state._closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        state.table_names()

    # Double close is safe
    state.close()
    assert state._closed is True


def test_factory_closes_state_on_tool_registration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv_dir = tmp_path / 'tool_fail_srv'
    srv_dir.mkdir(parents=True, mode=0o700)
    dl_dir = srv_dir / 'dl'
    dl_dir.mkdir(parents=True, mode=0o700)

    config = ServiceConfig(
        service_id='gmail',
        public_url='https://mcp.example.test/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=dl_dir,
        oauth_state_path=srv_dir / 'oauth.db',
        google_token_path=srv_dir / 'token.json',
        audit_log_path=srv_dir / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )

    class FailingExtension(Extension):
        def register_tools(self, registrar: ToolRegistrar) -> None:
            raise RuntimeError('tool registration boom')

    created_states: list[OAuthState] = []
    original_init = OAuthState.__init__

    def spy_init(self: OAuthState, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        created_states.append(self)

    monkeypatch.setattr(OAuthState, '__init__', spy_init)

    with pytest.raises(RuntimeError, match='tool registration boom'):
        create_service_app(config, extensions=[FailingExtension()])

    assert created_states == []
    assert not config.oauth_state_path.exists()


def test_factory_closes_state_on_build_app_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv_dir = tmp_path / 'build_fail_srv'
    srv_dir.mkdir(parents=True, mode=0o700)
    dl_dir = srv_dir / 'dl'
    dl_dir.mkdir(parents=True, mode=0o700)

    config = ServiceConfig(
        service_id='gmail',
        public_url='https://mcp.example.test/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=dl_dir,
        oauth_state_path=srv_dir / 'oauth.db',
        google_token_path=srv_dir / 'token.json',
        audit_log_path=srv_dir / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )

    class ConflictingRouteExtension(Extension):
        def register_routes(self, app: Any) -> None:
            async def conflict_handler(_: Request) -> JSONResponse:
                return JSONResponse({'conflict': 'yes'})

            app.routes.append(
                Route('/gmail/mcp', conflict_handler, methods=['POST'])
            )

    closed_states: list[OAuthState] = []
    original_close = OAuthState.close

    def spy_close(self: OAuthState) -> None:
        closed_states.append(self)
        original_close(self)

    monkeypatch.setattr(OAuthState, 'close', spy_close)
    with pytest.raises(ValueError, match='covers MCP transport path'):
        create_service_app(config, extensions=[ConflictingRouteExtension()])

    assert len(closed_states) == 1
    failed_state = closed_states[0]
    assert failed_state._closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        failed_state.table_names()


def test_factory_owns_exact_endpoint_resource(tmp_path: Path) -> None:
    srv_dir = tmp_path / 'exact_srv'
    srv_dir.mkdir(parents=True, mode=0o700)
    dl_dir = srv_dir / 'dl'
    dl_dir.mkdir(parents=True, mode=0o700)

    config = ServiceConfig(
        service_id='gmail',
        public_url='https://mcp.example.test/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=dl_dir,
        oauth_state_path=srv_dir / 'oauth.db',
        google_token_path=srv_dir / 'token.json',
        audit_log_path=srv_dir / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )
    _, _, state = create_service_app(config)
    try:
        assert state.resource == config.resource_url
        assert state.resource != config.public_url
    finally:
        state.close()
