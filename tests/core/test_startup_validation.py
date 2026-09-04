"""Startup validation behavior tests."""

import importlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.transport.extensions import Extension

PORTS = {
    'gmail': '8431',
    'calendar': '8432',
    'drive': '8433',
    'sheets': '8434',
    'docs': '8435',
}


def _setup_service_env(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    service: str,
) -> tuple[Path, Path, Path, Path]:
    """Configure service environment."""
    port = PORTS[service]
    prefix = service.upper()
    base_dir = root / service
    base_dir.mkdir(parents=True, mode=0o700)
    dl_dir = base_dir / 'downloads'
    dl_dir.mkdir(parents=True, mode=0o700)
    state_path = base_dir / 'state.sqlite3'
    token_path = base_dir / 'token.json'
    audit_path = base_dir / 'audit.jsonl'
    for key, value in {
        f'{prefix}_MCP_HOST': '127.0.0.1',
        f'{prefix}_MCP_PORT': port,
        f'{prefix}_MCP_PUBLIC_URL': f'https://mcp.example.test/{service}',
        f'{prefix}_MCP_PATH': f'/{service}/mcp',
        f'{prefix}_MCP_DOWNLOAD_PATH': str(dl_dir),
        f'{prefix}_OAUTH_STATE_PATH': str(state_path),
        f'{prefix}_GOOGLE_TOKEN_PATH': str(token_path),
        f'{prefix}_AUDIT_LOG_PATH': str(audit_path),
        f'{prefix}_OAUTH_LOGIN_USERNAME': 'test-admin',
        f'{prefix}_OAUTH_LOGIN_PASSWORD': 'test-password',
        f'{prefix}_MCP_FORWARDED_ALLOW_IPS': '127.0.0.1',
    }.items():
        monkeypatch.setenv(key, value)
    return base_dir, dl_dir, state_path, audit_path


@pytest.mark.parametrize('service', SERVICES)
def test_startup_validation_rejects_download_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
) -> None:
    _, dl_dir, _, _ = _setup_service_env(monkeypatch, tmp_path, service)
    prefix = service.upper()
    monkeypatch.setenv(f'{prefix}_AUDIT_LOG_PATH', str(dl_dir / 'audit.jsonl'))
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='download_path collision'):
        mod.run_server()


@pytest.mark.parametrize('service', SERVICES)
def test_startup_validation_rejects_symlink_audit_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
) -> None:
    base_dir, _, _, audit_path = _setup_service_env(
        monkeypatch, tmp_path, service
    )
    real_target = base_dir / 'real_audit.jsonl'
    real_target.touch(mode=0o600)
    audit_path.symlink_to(real_target)
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='invalid audit file target'):
        mod.run_server()


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize(
    'credential_var',
    ['OAUTH_LOGIN_USERNAME', 'OAUTH_LOGIN_PASSWORD'],
)
def test_startup_validation_rejects_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    credential_var: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    prefix = service.upper()
    monkeypatch.delenv(f'{prefix}_{credential_var}', raising=False)
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match=credential_var):
        mod.run_server()


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize(
    'wildcard_value',
    ['*', '*, 127.0.0.1', '127.0.0.1, *', '10.0.0.*'],
)
def test_startup_validation_rejects_wildcard_forwarded_allow_ips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    wildcard_value: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    prefix = service.upper()
    monkeypatch.setenv(f'{prefix}_MCP_FORWARDED_ALLOW_IPS', wildcard_value)
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='wildcard'):
        mod.run_server()


@pytest.mark.parametrize('service', SERVICES)
def test_startup_validation_rejects_state_and_token_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
) -> None:
    base_dir, _, _, _ = _setup_service_env(monkeypatch, tmp_path, service)
    prefix = service.upper()
    shared_path = base_dir / 'shared_file.db'
    monkeypatch.setenv(f'{prefix}_OAUTH_STATE_PATH', str(shared_path))
    monkeypatch.setenv(f'{prefix}_GOOGLE_TOKEN_PATH', str(shared_path))
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='must differ'):
        mod.run_server()


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize('invalid_port', ['0', '70000', 'not-a-port', '-1'])
def test_startup_validation_rejects_invalid_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    invalid_port: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    prefix = service.upper()
    monkeypatch.setenv(f'{prefix}_MCP_PORT', invalid_port)
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='MCP_PORT'):
        mod.run_server()


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize('invalid_path', ['mcp', '/mcp/../bad', ''])
def test_startup_validation_rejects_invalid_mcp_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    invalid_path: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    prefix = service.upper()
    monkeypatch.setenv(f'{prefix}_MCP_PATH', invalid_path)
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='MCP_PATH|MCP path|service identity'):
        mod.run_server()


class _CollidingExtension(Extension):
    """Model test extension."""

    def __init__(self, colliding_path: str, method: str = 'GET') -> None:
        """Initialize test double."""
        self._colliding_path = colliding_path
        self._method = method

    def register_routes(self, app: Any) -> None:
        """Register test routes."""

        async def dummy_endpoint(_: Request) -> JSONResponse:
            """Provide dummy endpoint."""
            return JSONResponse({'status': 'collision'})

        app.routes.append(
            Route(
                self._colliding_path,
                dummy_endpoint,
                methods=[self._method],
            )
        )


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize(
    'colliding_template',
    [
        '/health',
        '/{service}/oauth/token',
        '/.well-known/oauth-authorization-server/{service}',
    ],
)
def test_startup_validation_rejects_route_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    colliding_template: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    colliding_path = colliding_template.format(service=service)
    ext = _CollidingExtension(colliding_path)
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='covers auth-exempt path'):
        mod.run_server(extensions=[ext])


@pytest.mark.parametrize('service', SERVICES)
def test_startup_validation_rejects_mcp_route_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    ext = _CollidingExtension(f'/{service}/mcp', method='POST')
    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(ValueError, match='covers MCP transport path'):
        mod.run_server(extensions=[ext])


@pytest.mark.parametrize('service', SERVICES)
def test_startup_success_invokes_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    recorded_calls: list[dict[str, Any]] = []
    captured_state: list[Any] = []

    def mock_uvicorn_run(app: Any, **kwargs: Any) -> None:
        """Record server startup."""
        recorded_calls.append({'app': app, **kwargs})
        state = app.user_middleware[0].kwargs['oauth_state']
        captured_state.append(state)
        assert not state._closed
        assert isinstance(state.table_names(), set)

    import uvicorn

    monkeypatch.setattr(uvicorn, 'run', mock_uvicorn_run)

    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    mod.run_server()

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call['host'] == '127.0.0.1'
    assert call['port'] == int(PORTS[service])
    assert call['proxy_headers'] is True
    assert call['forwarded_allow_ips'] == ['127.0.0.1']
    assert '*' not in call['forwarded_allow_ips']
    assert hasattr(call['app'], 'routes')
    assert len(captured_state) == 1
    state = captured_state[0]
    assert state._closed is True
    with pytest.raises(
        sqlite3.ProgrammingError, match='Cannot operate on a closed database'
    ):
        state.table_names()


@pytest.mark.parametrize('service', SERVICES)
def test_startup_uvicorn_exception_closes_oauth_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
) -> None:
    _setup_service_env(monkeypatch, tmp_path, service)
    captured_state: list[Any] = []

    def mock_uvicorn_run(app: Any, **kwargs: Any) -> None:
        """Record server startup."""
        state = app.user_middleware[0].kwargs['oauth_state']
        captured_state.append(state)
        assert not state._closed
        assert isinstance(state.table_names(), set)
        raise RuntimeError('simulated uvicorn startup failure')

    import uvicorn

    monkeypatch.setattr(uvicorn, 'run', mock_uvicorn_run)

    mod = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(
        RuntimeError, match='simulated uvicorn startup failure'
    ):
        mod.run_server()
    assert len(captured_state) == 1
    state = captured_state[0]
    assert state._closed is True
    with pytest.raises(
        sqlite3.ProgrammingError, match='Cannot operate on a closed database'
    ):
        state.table_names()
