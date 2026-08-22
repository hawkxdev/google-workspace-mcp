"""Test OAuth resource URLs."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.oauth import protected_resource_metadata_url
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig

RESOURCE = 'https://mcp.example.test/gmail/mcp'
RESOURCE_METADATA = (
    'https://mcp.example.test/.well-known/oauth-protected-resource/gmail/mcp'
)
SPOOFED_HOST = 'evil.example'
SPOOFED_HEADERS = {
    'Host': SPOOFED_HOST,
    'X-Forwarded-Host': SPOOFED_HOST,
    'X-Forwarded-Proto': 'https',
}


@pytest.fixture
def service_config(state_dir: Path) -> ServiceConfig:
    """Build pinned service config."""
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
    """Build pinned OAuth state."""
    state = OAuthState(
        service_config.oauth_state_path,
        download_path=tmp_path / 'downloads',
        service_id=service_config.service_id,
        resource=service_config.public_url,
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
    """Build advertised URL client."""

    async def protected(_: Request) -> PlainTextResponse:
        """Return protected response."""
        return PlainTextResponse('protected')

    async def metadata(_: Request) -> PlainTextResponse:
        """Return metadata response."""
        return PlainTextResponse('metadata')

    app = Starlette(
        routes=[
            Route(service_config.mcp_path, protected),
            Route(
                '/.well-known/oauth-protected-resource/gmail/mcp',
                metadata,
            ),
            Route(
                '/.well-known/oauth-authorization-server/gmail/mcp',
                metadata,
            ),
            Route('/gmail/mcp/oauth/authorize', metadata),
            Route('/gmail/mcp/oauth/token', metadata),
            Route('/gmail/mcp/oauth/register', metadata),
        ]
    )
    app.add_middleware(
        BearerAuthMiddleware,
        config=service_config,
        oauth_state=oauth_state,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_resource_metadata_url_inserts_well_known_before_path() -> None:
    assert protected_resource_metadata_url(RESOURCE) == RESOURCE_METADATA


def test_resource_metadata_url_uses_default_path_for_origin() -> None:
    assert protected_resource_metadata_url('https://mcp.example.test') == (
        'https://mcp.example.test/.well-known/oauth-protected-resource'
    )


@pytest.mark.parametrize(
    'resource',
    [
        'http://mcp.example.test/gmail/mcp',
        'relative/gmail/mcp',
        'https:///gmail/mcp',
        'https://mcp.example.test/gmail/mcp#fragment',
    ],
)
def test_resource_metadata_url_rejects_invalid_resource(
    resource: str,
) -> None:
    with pytest.raises(ValueError, match='resource'):
        protected_resource_metadata_url(resource)


def test_resource_metadata_url_removes_authority_trailing_slash() -> None:
    assert protected_resource_metadata_url('https://mcp.example.test/') == (
        'https://mcp.example.test/.well-known/oauth-protected-resource'
    )


def test_percent_encoded_resource_metadata_route_is_public(
    tmp_path: Path,
) -> None:
    # 1. Build encoded config
    resource = 'https://mcp.example.test/gmail%2Fmcp'
    config = ServiceConfig(
        service_id='gmail',
        public_url=resource,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        oauth_state_path=tmp_path / 'encoded.sqlite3',
        google_token_path=tmp_path / 'google-token.json',
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )
    # 2. Build protected app
    state = OAuthState(
        config.oauth_state_path,
        download_path=tmp_path / 'downloads',
        service_id=config.service_id,
        resource=config.public_url,
    )

    async def metadata(_: Request) -> PlainTextResponse:
        """Return metadata response."""
        return PlainTextResponse('metadata')

    app = Starlette(
        routes=[
            Route(
                '/.well-known/oauth-protected-resource/gmail/mcp',
                metadata,
            )
        ]
    )
    app.add_middleware(
        BearerAuthMiddleware,
        config=config,
        oauth_state=state,
    )
    # 3. Request encoded metadata
    try:
        with TestClient(app) as test_client:
            response = test_client.get(
                '/.well-known/oauth-protected-resource/gmail%2Fmcp'
            )
    finally:
        state.close()

    # 4. Verify public response
    assert response.status_code == 200
    assert response.text == 'metadata'


def test_challenge_ignores_spoofed_host(client: TestClient) -> None:
    response = client.get('/gmail/mcp', headers=SPOOFED_HEADERS)

    assert response.status_code == 401
    challenge = response.headers['WWW-Authenticate']
    assert f'resource_metadata="{RESOURCE_METADATA}"' in challenge
    assert SPOOFED_HOST not in challenge


def test_resource_metadata_route_is_public(client: TestClient) -> None:
    response = client.get(
        '/.well-known/oauth-protected-resource/gmail/mcp',
        headers=SPOOFED_HEADERS,
    )

    assert response.status_code == 200
    assert response.text == 'metadata'


def test_server_metadata_route_is_public(client: TestClient) -> None:
    response = client.get(
        '/.well-known/oauth-authorization-server/gmail/mcp',
        headers=SPOOFED_HEADERS,
    )

    assert response.status_code == 200
    assert response.text == 'metadata'


@pytest.mark.parametrize(
    'path',
    [
        '/gmail/mcp/oauth/authorize',
        '/gmail/mcp/oauth/token',
        '/gmail/mcp/oauth/register',
    ],
)
def test_operational_oauth_routes_are_public(
    client: TestClient, path: str
) -> None:
    response = client.get(path, headers=SPOOFED_HEADERS)

    assert response.status_code == 200
    assert response.text == 'metadata'


def test_forwarded_allow_ips_default_is_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('GMAIL_MCP_FORWARDED_ALLOW_IPS', raising=False)

    config = ServiceConfig.from_env('gmail')

    assert config.forwarded_allow_ips == ('127.0.0.1',)
    assert '*' not in config.forwarded_allow_ips
