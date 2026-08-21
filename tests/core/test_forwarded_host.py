"""Test advertised OAuth resource URLs."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_workspace_mcp.auth.bearer import (
    BearerAuthMiddleware,
    protected_resource_metadata_url,
)
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


def test_forwarded_allow_ips_default_is_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('GMAIL_MCP_FORWARDED_ALLOW_IPS', raising=False)

    config = ServiceConfig.from_env('gmail')

    assert config.forwarded_allow_ips == ('127.0.0.1',)
    assert '*' not in config.forwarded_allow_ips
