"""Tests for transport and audit hardening."""

import base64
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_workspace_mcp.audit.logger import (
    AuditError,
    AuditLogger,
    validate_audit_path,
)
from google_workspace_mcp.auth.oauth import OAuthEndpoints
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import (
    ServiceConfig,
    validate_forwarded_allow_ips,
)
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.factory import create_service_app
from google_workspace_mcp.transport.server import build_app

RESOURCE = 'https://mcp.example.test/gmail/mcp'
PUBLIC_HOST = 'mcp.example.test'
REDIRECT_URI = 'https://client.example.test/callback'
CODE_VERIFIER = 'pkce-verifier-marker-long-enough'
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
    .rstrip(b'=')
    .decode()
)
INITIALIZE_BODY = {
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'probe', 'version': '1'},
    },
}
MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}


def _config(tmp_path: Path, **overrides: object) -> ServiceConfig:
    """Build a service configuration."""
    state_dir = tmp_path / 'state'
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    download_dir = tmp_path / 'downloads'
    download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    values: dict[str, object] = {
        'service_id': 'gmail',
        'public_url': RESOURCE,
        'mcp_path': '/gmail/mcp',
        'host': '127.0.0.1',
        'port': 8431,
        'download_path': download_dir,
        'oauth_state_path': state_dir / 'oauth_state.sqlite3',
        'google_token_path': state_dir / 'google_token.json',
        'audit_log_path': state_dir / 'audit.jsonl',
        'oauth_login_username': 'admin',
        'oauth_login_password': 'secret-password',
        'allowed_hosts': (PUBLIC_HOST,),
        'forwarded_allow_ips': ('127.0.0.1',),
        'legacy_clients_path': None,
        'approved_legacy_client_ids': frozenset(),
        'access_token_ttl_seconds': 86400,
        'refresh_token_ttl_seconds': 2592000,
    }
    values.update(overrides)
    return ServiceConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def config(tmp_path: Path) -> ServiceConfig:
    return _config(tmp_path)


@pytest.fixture
def state(config: ServiceConfig) -> Iterator[OAuthState]:
    with OAuthState(
        config.oauth_state_path,
        service_id=config.service_id,
        resource=config.public_url,
        download_path=config.download_path,
    ) as opened:
        yield opened


class _RouteExtension(Extension):
    """Extension registering one route."""

    def __init__(self, path: str, methods: list[str]) -> None:
        self._path = path
        self._methods = methods

    def register_tools(self, registrar: ToolRegistrar) -> None:
        pass

    def register_routes(self, app: object) -> None:
        async def handler(request: object) -> PlainTextResponse:
            return PlainTextResponse('extension')

        app.routes.append(  # type: ignore[attr-defined]
            Route(self._path, handler, methods=self._methods)
        )

    def shutdown(self) -> None:
        pass


# Transport host binding


def _issue_access_token(state: OAuthState) -> str:
    """Issue one readonly access token."""
    registered = state.register_client((REDIRECT_URI,))
    code = state.issue_authorization_code(
        client_id=registered.client.client_id,
        redirect_uri=REDIRECT_URI,
        code_challenge=CODE_CHALLENGE,
        resource=RESOURCE,
    )
    issued = state.redeem_authorization_code(
        code=code,
        client_id=registered.client.client_id,
        client_secret=registered.client_secret,
        redirect_uri=REDIRECT_URI,
        code_verifier=CODE_VERIFIER,
        resource=RESOURCE,
    )
    return issued.access_token


def test_configured_public_host_reaches_mcp_transport(
    config: ServiceConfig, state: OAuthState
) -> None:
    app = build_app(config, state, PolicyMCPServer('gmail'))
    auth = {'Authorization': f'Bearer {_issue_access_token(state)}'}

    with TestClient(app) as client:
        allowed = client.post(
            config.mcp_path,
            json=INITIALIZE_BODY,
            headers={**MCP_HEADERS, **auth, 'Host': PUBLIC_HOST},
        )
        rejected = client.post(
            config.mcp_path,
            json=INITIALIZE_BODY,
            headers={**MCP_HEADERS, **auth, 'Host': 'attacker.example.test'},
        )

    assert allowed.status_code == 200
    assert rejected.status_code == 421


def test_unconfigured_host_keeps_loopback_boundary(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, allowed_hosts=())
    with OAuthState(
        config.oauth_state_path,
        service_id=config.service_id,
        resource=config.public_url,
        download_path=config.download_path,
    ) as state:
        app = build_app(config, state, PolicyMCPServer('gmail'))
        auth = {'Authorization': f'Bearer {_issue_access_token(state)}'}
        with TestClient(app) as client:
            response = client.post(
                config.mcp_path,
                json=INITIALIZE_BODY,
                headers={**MCP_HEADERS, **auth, 'Host': PUBLIC_HOST},
            )

    assert response.status_code == 421


# Tool capability propagation


def test_registered_tool_capabilities_reach_oauth_state(
    config: ServiceConfig,
) -> None:
    class ToolExtension(Extension):
        def register_tools(self, registrar: ToolRegistrar) -> None:
            @registrar.tool(
                name='search_messages',
                required_capability='mail.read',
                available_to_readonly=True,
            )
            def search_messages() -> str:
                return 'ok'

        def register_routes(self, app: object) -> None:
            pass

        def shutdown(self) -> None:
            pass

    _, server, state = create_service_app(config, extensions=[ToolExtension()])
    try:
        assert server.required_capability('search_messages') == 'mail.read'
        assert state.readonly_capabilities == ('mail.read',)
    finally:
        state.close()


# Refresh rotation atomicity


def test_failed_rotation_audit_keeps_refresh_token_usable(
    config: ServiceConfig, state: OAuthState
) -> None:
    registered = state.register_client((REDIRECT_URI,))
    client_id = registered.client.client_id
    code = state.issue_authorization_code(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        code_challenge=CODE_CHALLENGE,
        resource=RESOURCE,
    )
    issued = state.redeem_authorization_code(
        code=code,
        client_id=client_id,
        client_secret=registered.client_secret,
        redirect_uri=REDIRECT_URI,
        code_verifier=CODE_VERIFIER,
        resource=RESOURCE,
    )
    refresh_token = issued.refresh_token
    assert refresh_token is not None

    def failing_writer(record: dict[str, object]) -> None:
        raise AuditError('Failed to record audit event')

    endpoints = OAuthEndpoints(
        config=config,
        oauth_state=state,
        login_username=config.oauth_login_username,
        login_password=config.oauth_login_password,
        audit_writer=failing_writer,
    )

    with pytest.raises(AuditError):
        endpoints._rotate_and_audit(refresh_token, client_id, RESOURCE)

    rows = state.list_refresh_tokens()
    assert [row.consumed_at for row in rows] == [None]
    assert [row.revoked_at for row in rows] == [None]

    written: list[dict[str, object]] = []
    working = OAuthEndpoints(
        config=config,
        oauth_state=state,
        login_username=config.oauth_login_username,
        login_password=config.oauth_login_password,
        audit_writer=written.append,
    )
    rotated = working._rotate_and_audit(refresh_token, client_id, RESOURCE)

    assert rotated.refresh_token is not None
    assert len(written) == 1


# Audit path boundaries


def test_validate_audit_path_rejects_untrusted_symlink_ancestor(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / 'downloads'
    downloads.mkdir(mode=0o700)
    elsewhere = tmp_path / 'elsewhere'
    (elsewhere / 'sub').mkdir(mode=0o700, parents=True)
    boundary = tmp_path / 'boundary'
    boundary.mkdir(mode=0o700)
    (boundary / 'link').symlink_to(elsewhere)
    os.chmod(boundary, 0o777)

    with pytest.raises(ValueError, match='insecure audit path ancestor'):
        validate_audit_path(
            boundary / 'link' / 'sub' / 'audit.jsonl', downloads
        )


def test_validate_audit_path_rejects_group_or_world_accessible_directory(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / 'downloads'
    downloads.mkdir(mode=0o700)
    loose = tmp_path / 'loose'
    loose.mkdir(mode=0o777)
    os.chmod(loose, 0o777)

    with pytest.raises(ValueError, match='insecure audit directory mode'):
        validate_audit_path(loose / 'audit.jsonl', downloads)


def test_audit_logger_rejects_existing_open_directory(
    tmp_path: Path,
) -> None:
    loose = tmp_path / 'loose'
    loose.mkdir(mode=0o777)
    os.chmod(loose, 0o777)

    with pytest.raises(AuditError, match='Insecure directory'):
        AuditLogger(loose / 'audit.jsonl').log_event({'op': 'probe'})

    assert stat.S_IMODE(os.lstat(loose).st_mode) == 0o777


def test_short_write_completes_the_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / 'state' / 'audit.jsonl'
    real_write = os.write

    def short_write(fd: int, data: object) -> int:
        return real_write(fd, bytes(data)[:8])  # type: ignore[arg-type]

    monkeypatch.setattr(os, 'write', short_write)
    AuditLogger(target).log_event({'op': 'oauth_refresh_rotation'})

    assert json.loads(target.read_text(encoding='utf-8')) == {
        'op': 'oauth_refresh_rotation'
    }


def test_stalled_write_is_reported_as_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / 'state' / 'audit.jsonl'

    def stalled_write(fd: int, data: object) -> int:
        return 0

    monkeypatch.setattr(os, 'write', stalled_write)

    with pytest.raises(AuditError):
        AuditLogger(target).log_event({'op': 'oauth_refresh_rotation'})


# Extension route boundaries


@pytest.mark.parametrize(
    'path, methods',
    [
        ('/ready', ['POST']),
        ('/gmail/mcp/oauth/token', ['GET']),
        ('/.well-known/oauth-protected-resource/gmail/mcp', ['GET']),
    ],
)
def test_extension_cannot_cover_authenticated_or_public_routes(
    config: ServiceConfig,
    state: OAuthState,
    path: str,
    methods: list[str],
) -> None:
    with pytest.raises(ValueError, match='auth-exempt path'):
        build_app(
            config,
            state,
            PolicyMCPServer('gmail'),
            extensions=[_RouteExtension(path, methods)],
        )


def test_prepared_extensions_are_released_on_construction_failure(
    config: ServiceConfig,
) -> None:
    events: list[str] = []

    class PreparedExtension(Extension):
        def register_tools(self, registrar: ToolRegistrar) -> None:
            events.append('prepared')

        def register_routes(self, app: object) -> None:
            pass

        def shutdown(self) -> None:
            events.append('shutdown')

    class CollidingExtension(_RouteExtension):
        def __init__(self) -> None:
            super().__init__('/health', ['GET'])

        def shutdown(self) -> None:
            events.append('colliding-shutdown')

    with pytest.raises(ValueError, match='auth-exempt path'):
        create_service_app(
            config,
            extensions=[PreparedExtension(), CollidingExtension()],
        )

    assert events == ['prepared', 'colliding-shutdown', 'shutdown']


def test_extensions_are_released_on_early_factory_failures(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class TrackedExtension(Extension):
        def register_tools(self, registrar: ToolRegistrar) -> None:
            events.append('register')
            raise RuntimeError('registration failed')

        def shutdown(self) -> None:
            events.append('shutdown')

    invalid = _config(tmp_path / 'invalid', public_url='http://localhost')
    with pytest.raises(ValueError, match='HTTPS'):
        create_service_app(invalid, extensions=[TrackedExtension()])
    assert events == ['shutdown']

    events.clear()
    valid = _config(tmp_path / 'valid')
    with pytest.raises(RuntimeError, match='registration failed'):
        create_service_app(valid, extensions=[TrackedExtension()])
    assert events == ['register', 'shutdown']


# Startup validation


def test_factory_rejects_audit_path_colliding_with_state_or_token(
    tmp_path: Path,
) -> None:
    state_collision = _config(
        tmp_path / 'a',
        audit_log_path=(tmp_path / 'a' / 'state' / 'oauth_state.sqlite3'),
    )
    token_collision = _config(
        tmp_path / 'b',
        audit_log_path=(tmp_path / 'b' / 'state' / 'google_token.json'),
    )
    state_token_collision = _config(
        tmp_path / 'c',
        google_token_path=(tmp_path / 'c' / 'state' / 'oauth_state.sqlite3'),
    )

    with pytest.raises(ValueError, match='oauth_state_path must differ'):
        create_service_app(state_collision)
    with pytest.raises(ValueError, match='google_token_path must differ'):
        create_service_app(token_collision)
    with pytest.raises(
        ValueError, match='oauth_state_path and google_token_path'
    ):
        create_service_app(state_token_collision)

    assert not state_collision.oauth_state_path.exists()
    assert not token_collision.oauth_state_path.exists()
    assert not state_token_collision.oauth_state_path.exists()


def test_factory_rejects_state_paths_through_symlink_alias(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'alias'
    state_dir = root / 'state'
    state_dir.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    alias = tmp_path / 'state-link'
    alias.symlink_to(state_dir, target_is_directory=True)
    config = _config(
        root,
        google_token_path=alias / 'oauth_state.sqlite3',
    )
    with pytest.raises(
        ValueError, match='oauth_state_path and google_token_path'
    ):
        create_service_app(config)
    assert not config.oauth_state_path.exists()


def test_factory_rejects_invalid_public_url_before_touching_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, public_url='http://127.0.0.1:8431')

    with pytest.raises(ValueError, match='HTTPS'):
        create_service_app(config)

    assert not config.oauth_state_path.exists()


def test_public_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GMAIL_OAUTH_LOGIN_USERNAME', 'admin')
    monkeypatch.setenv('GMAIL_OAUTH_LOGIN_PASSWORD', 'secret-password')
    monkeypatch.delenv('GMAIL_MCP_PUBLIC_URL', raising=False)

    with pytest.raises(ValueError, match='GMAIL_MCP_PUBLIC_URL'):
        ServiceConfig.from_env('gmail')


@pytest.mark.parametrize('entry', ['0.0.0.0/0', '::/0', '0.0.0.0', '*'])
def test_unbounded_proxy_entries_are_rejected(entry: str) -> None:
    with pytest.raises(ValueError, match='FORWARDED_ALLOW_IPS'):
        validate_forwarded_allow_ips((entry,), 'GMAIL')


# Public endpoint input bounds


def test_oversized_client_id_is_rejected_before_audit(
    config: ServiceConfig, state: OAuthState
) -> None:
    app = build_app(config, state, PolicyMCPServer('gmail'))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        '/gmail/mcp/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': 'irrelevant',
            'client_id': 'A' * 262144,
            'resource': 'https://other.example.test/',
        },
        headers={'Host': PUBLIC_HOST},
    )

    assert response.status_code == 400
    assert response.json()['error'] == 'invalid_request'
    assert not config.audit_log_path.exists()
