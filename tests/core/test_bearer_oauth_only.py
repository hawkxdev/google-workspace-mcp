"""Test OAuth-only bearer authentication."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.context import (
    current_principal,
    current_request_context,
)
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig

RESOURCE = 'https://mcp.example.test/gmail/mcp'
FOREIGN_RESOURCE = 'https://mcp.example.test/drive/mcp'
RESOURCE_METADATA = (
    'https://mcp.example.test/.well-known/oauth-protected-resource/gmail/mcp'
)
READONLY_CAPABILITIES = ('gmail.read',)


@pytest.fixture
def service_config(state_dir: Path) -> ServiceConfig:
    """Build isolated service config."""
    return ServiceConfig(
        service_id='gmail',
        public_url=RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        oauth_state_path=state_dir / 'oauth.sqlite3',
        google_token_path=state_dir / 'google-token.json',
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


@pytest.fixture
def client(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> Iterator[TestClient]:
    """Build protected test client."""

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
    with TestClient(app) as test_client:
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
        '/oauth/token',
        '/.well-known/oauth-protected-resource/gmail/mcp',
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


def test_missing_auth_returns_rfc9728_challenge(
    client: TestClient,
) -> None:
    response = client.get('/gmail/mcp')

    assert response.status_code == 401
    assert response.json() == {
        'error': 'Missing or malformed Authorization header'
    }
    challenge = response.headers['WWW-Authenticate']
    assert challenge.startswith('Bearer ')
    assert f'resource_metadata="{RESOURCE_METADATA}"' in challenge
    assert 'error="invalid_request"' in challenge


@pytest.mark.parametrize('authorization', ['Basic value', 'Bearer', 'Bearer '])
def test_malformed_auth_returns_invalid_request(
    client: TestClient,
    authorization: str,
) -> None:
    response = client.get(
        '/gmail/mcp', headers={'Authorization': authorization}
    )

    assert response.status_code == 401
    assert 'error="invalid_request"' in response.headers['WWW-Authenticate']


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
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('GMAIL_MCP_TOKEN', 'secret-token')

    response = client.get(
        '/gmail/mcp', headers={'Authorization': 'Bearer secret-token'}
    )

    assert response.status_code == 401
    assert response.json() == {'error': 'Invalid token'}
