"""Test OAuth resource isolation."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import google_workspace_mcp.common.config as config_module
from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig

GMAIL_RESOURCE = 'https://mcp.example.test/gmail/mcp'
DRIVE_RESOURCE = 'https://mcp.example.test/drive/mcp'
SERVICES = ('gmail', 'calendar', 'drive', 'sheets', 'docs')
READONLY_CAPABILITIES = ('gmail.read',)
REDIRECT_URI = 'https://client.example.test/oauth/callback'


@pytest.fixture
def service_config(state_dir: Path) -> ServiceConfig:
    """Build Gmail service config."""
    download_path = state_dir / 'downloads'
    download_path.mkdir()
    return ServiceConfig(
        service_id='gmail',
        public_url=GMAIL_RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=download_path,
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
def oauth_state(service_config: ServiceConfig) -> Iterator[OAuthState]:
    """Build Gmail OAuth state."""
    with OAuthState(
        service_config.oauth_state_path,
        download_path=service_config.download_path,
        service_id=service_config.service_id,
        resource=service_config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
        access_token_ttl_seconds=service_config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=service_config.refresh_token_ttl_seconds,
    ) as state:
        yield state


def test_issued_token_persists_service_resource(
    oauth_state: OAuthState,
) -> None:
    registered = oauth_state.register_client((REDIRECT_URI,))
    issued = oauth_state.issue_access_token(
        client_id=registered.client.client_id,
        resource=GMAIL_RESOURCE,
    )

    metadata = oauth_state.lookup_access_token(issued.access_token)

    assert metadata is not None
    assert metadata.resource == GMAIL_RESOURCE


def test_bearer_rejects_foreign_resource_present_in_local_state(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    registered = oauth_state.register_client((REDIRECT_URI,))
    issued = oauth_state.issue_access_token(
        client_id=registered.client.client_id,
        resource=GMAIL_RESOURCE,
    )
    with sqlite3.connect(service_config.oauth_state_path) as connection:
        connection.execute(
            'UPDATE access_tokens SET resource = ? WHERE token_id = ?',
            (DRIVE_RESOURCE, issued.token.token_id),
        )
    metadata = oauth_state.lookup_access_token(issued.access_token)
    assert metadata is not None
    assert metadata.resource == DRIVE_RESOURCE

    async def protected(_: Request) -> PlainTextResponse:
        """Return protected response."""
        return PlainTextResponse('accepted')

    app = Starlette(routes=[Route(service_config.mcp_path, protected)])
    app.add_middleware(
        BearerAuthMiddleware,
        config=service_config,
        oauth_state=oauth_state,
    )

    with TestClient(app) as client:
        response = client.get(
            service_config.mcp_path,
            headers={'Authorization': f'Bearer {issued.access_token}'},
        )

    assert response.status_code == 401
    assert response.json() == {'error': 'Invalid token'}
    assert 'error="invalid_token"' in response.headers['WWW-Authenticate']


def test_service_states_use_distinct_paths_and_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / 'state'
    state_root.mkdir(mode=0o700)
    monkeypatch.setattr(config_module, '_STATE_ROOT', state_root)
    for service in SERVICES:
        prefix = service.upper()
        monkeypatch.delenv(f'{prefix}_MCP_DOWNLOAD_PATH', raising=False)
        monkeypatch.delenv(f'{prefix}_OAUTH_STATE_PATH', raising=False)
        monkeypatch.delenv(f'{prefix}_GOOGLE_TOKEN_PATH', raising=False)
        monkeypatch.delenv(f'{prefix}_AUDIT_LOG_PATH', raising=False)
        monkeypatch.setenv(f'{prefix}_OAUTH_LOGIN_USERNAME', 'admin')
        monkeypatch.setenv(f'{prefix}_OAUTH_LOGIN_PASSWORD', 'test-password')

    configs = {
        service: ServiceConfig.from_env(service) for service in SERVICES
    }

    assert len({config.oauth_state_path for config in configs.values()}) == 5

    gmail = configs['gmail']
    gmail.oauth_state_path.parent.mkdir(mode=0o700)
    gmail.download_path.mkdir(mode=0o700)
    with OAuthState(
        gmail.oauth_state_path,
        download_path=gmail.download_path,
        service_id=gmail.service_id,
        resource=gmail.public_url,
    ) as gmail_state:
        registered = gmail_state.register_client((REDIRECT_URI,))
        issued = gmail_state.issue_access_token(
            client_id=registered.client.client_id,
            resource=gmail.public_url,
        )

    drive = configs['drive']
    drive.oauth_state_path.parent.mkdir(mode=0o700)
    drive.download_path.mkdir(mode=0o700)
    with OAuthState(
        drive.oauth_state_path,
        download_path=drive.download_path,
        service_id=drive.service_id,
        resource=drive.public_url,
    ) as drive_state:
        assert drive_state.lookup_access_token(issued.access_token) is None
