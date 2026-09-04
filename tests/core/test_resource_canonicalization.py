"""Resource URL canonicalization contracts."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from google_workspace_mcp.auth.oauth import OAuthEndpoints
from google_workspace_mcp.auth.state import (
    InvalidTarget,
    OAuthState,
    canonicalize_resource,
)
from google_workspace_mcp.common.config import ServiceConfig

LOGIN_USERNAME = 'test-user'
LOGIN_PASSWORD = 'test-pass'
ISSUER = 'https://mcp.example.test/gmail'
PATH_RESOURCE = 'https://mcp.example.test/gmail/mcp'
SERVICE_BASE = 'https://mcp.example.test/gmail'
PATH_SLASHED = 'https://mcp.example.test/gmail/mcp/'
PATH_DOUBLE_SLASH = 'https://mcp.example.test/gmail/mcp//'
ROOT_RESOURCE = 'https://mcp.example.test'
ROOT_SLASHED = 'https://mcp.example.test/'
ROOT_DOUBLE_SLASH = 'https://mcp.example.test//'
REDIRECT = 'https://client.example.test/callback'
READONLY = ('gmail_get_message', 'gmail_search')
VERIFIER = 'pkce-verifier-marker-long-enough'
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b'=')
    .decode()
)


# === Canonicalization helper cases ===


def test_canonical_form_collapses_only_the_root_slash() -> None:
    assert canonicalize_resource(ROOT_SLASHED) == ROOT_RESOURCE
    assert canonicalize_resource(ROOT_RESOURCE) == ROOT_RESOURCE
    assert canonicalize_resource(ROOT_DOUBLE_SLASH) == ROOT_DOUBLE_SLASH
    assert canonicalize_resource(PATH_RESOURCE) == PATH_RESOURCE
    assert canonicalize_resource(PATH_SLASHED) == PATH_SLASHED
    assert canonicalize_resource(PATH_DOUBLE_SLASH) == PATH_DOUBLE_SLASH
    assert canonicalize_resource('') == ''


# === Shared endpoint fixtures ===


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    """Build isolated endpoint configuration."""
    state_dir = tmp_path / 'state'
    state_dir.mkdir(mode=0o700, parents=True)
    return ServiceConfig(
        service_id='gmail',
        public_url=ISSUER,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=tmp_path / 'downloads',
        oauth_state_path=state_dir / 'oauth_state.sqlite3',
        google_token_path=state_dir / 'google.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username=LOGIN_USERNAME,
        oauth_login_password=LOGIN_PASSWORD,
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=tmp_path / 'legacy.json',
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=86400,
    )


@pytest.fixture
def oauth_state(
    service_config: ServiceConfig,
    tmp_path: Path,
) -> Iterator[OAuthState]:
    """Build isolated endpoint state."""
    downloads = tmp_path / 'downloads'
    downloads.mkdir(mode=0o700)
    state = OAuthState(
        service_config.oauth_state_path,
        download_path=downloads,
        service_id=service_config.service_id,
        resource=service_config.resource_url,
        readonly_capabilities=READONLY,
        legacy_path=service_config.legacy_clients_path,
        approved_legacy_client_ids=service_config.approved_legacy_client_ids,
        access_token_ttl_seconds=service_config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=service_config.refresh_token_ttl_seconds,
    )
    state.migrate_legacy()
    try:
        yield state
    finally:
        state.close()


@pytest.fixture
def endpoints(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> OAuthEndpoints:
    """Build OAuth endpoint bundle."""
    return OAuthEndpoints(
        config=service_config,
        oauth_state=oauth_state,
        login_username=LOGIN_USERNAME,
        login_password=LOGIN_PASSWORD,
        audit_writer=lambda _: None,
    )


@pytest.fixture
def endpoint(endpoints: OAuthEndpoints) -> TestClient:
    """Build Starlette test client."""
    app = Starlette(routes=endpoints.routes)
    return TestClient(app)


# === Endpoint flow helpers ===


def _register(
    endpoint: TestClient, oauth_path: str = '/gmail/oauth'
) -> tuple[str, str]:
    """Register dynamic test client."""
    response = endpoint.post(
        f'{oauth_path}/register',
        json={'client_name': 'Headless', 'redirect_uris': [REDIRECT]},
    )
    assert response.status_code == 201
    return response.json()['client_id'], response.json()['client_secret']


def _authorize(
    endpoint: TestClient,
    client_id: str,
    resource: str,
    oauth_path: str = '/gmail/oauth',
) -> str:
    """Execute authorization code flow."""
    response = endpoint.post(
        f'{oauth_path}/authorize',
        data={
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': resource,
            'username': LOGIN_USERNAME,
            'password': LOGIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    return response.headers['location'].split('code=')[1].split('&')[0]


def _exchange(
    endpoint: TestClient,
    client_id: str,
    client_secret: str,
    code: str,
    resource: str,
    oauth_path: str = '/gmail/oauth',
) -> dict:
    """Execute code exchange request."""
    response = endpoint.post(
        f'{oauth_path}/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': REDIRECT,
            'code_verifier': VERIFIER,
            'resource': resource,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _refresh(
    endpoint: TestClient,
    client_id: str,
    refresh_token: str,
    resource: str,
    oauth_path: str = '/gmail/oauth',
):
    """Execute refresh rotation request."""
    return endpoint.post(
        f'{oauth_path}/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client_id,
            'resource': resource,
        },
    )


# === Path resource exact matching tests ===


def test_path_resource_passes_full_flow(endpoint: TestClient) -> None:
    client_id, client_secret = _register(endpoint)
    code = _authorize(endpoint, client_id, PATH_RESOURCE)
    issued = _exchange(endpoint, client_id, client_secret, code, PATH_RESOURCE)
    rotated = _refresh(
        endpoint, client_id, issued['refresh_token'], PATH_RESOURCE
    )
    assert rotated.status_code == 200, rotated.text


@pytest.mark.parametrize(
    'bad_resource',
    [
        SERVICE_BASE,
        PATH_SLASHED,
        PATH_DOUBLE_SLASH,
        PATH_RESOURCE + '/extra',
        'https://mcp.example.test/drive/mcp',
        'https://MCP.example.test/gmail/mcp',
        'https://mcp.example.test/gmail%2Fmcp',
        ROOT_RESOURCE,
        ROOT_SLASHED,
    ],
)
def test_path_resource_variations_stay_rejected_everywhere(
    endpoint: TestClient,
    bad_resource: str,
) -> None:
    client_id, client_secret = _register(endpoint)
    denied_auth = endpoint.post(
        '/gmail/oauth/authorize',
        data={
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': bad_resource,
            'username': LOGIN_USERNAME,
            'password': LOGIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert denied_auth.status_code == 400
    assert denied_auth.json()['error'] == 'invalid_target'

    code = _authorize(endpoint, client_id, PATH_RESOURCE)
    exchange = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': REDIRECT,
            'code_verifier': VERIFIER,
            'resource': bad_resource,
        },
    )
    assert exchange.status_code == 400
    assert exchange.json()['error'] == 'invalid_target'

    code = _authorize(endpoint, client_id, PATH_RESOURCE)
    issued = _exchange(endpoint, client_id, client_secret, code, PATH_RESOURCE)
    rotated = _refresh(
        endpoint, client_id, issued['refresh_token'], bad_resource
    )
    assert rotated.status_code == 400
    assert rotated.json()['error'] == 'invalid_target'


def test_duplicate_resource_parameter_rejected_everywhere(
    endpoint: TestClient,
) -> None:
    client_id, client_secret = _register(endpoint)
    denied_auth = endpoint.post(
        '/gmail/oauth/authorize',
        data={
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': [PATH_RESOURCE, PATH_RESOURCE],
            'username': LOGIN_USERNAME,
            'password': LOGIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert denied_auth.status_code == 400
    assert denied_auth.json()['error'] == 'invalid_request'

    denied_get = endpoint.get(
        '/gmail/oauth/authorize',
        params={
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': [PATH_RESOURCE, PATH_RESOURCE],
        },
    )
    assert denied_get.status_code == 400
    assert denied_get.json()['error'] == 'invalid_request'

    code = _authorize(endpoint, client_id, PATH_RESOURCE)
    exchange = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': REDIRECT,
            'code_verifier': VERIFIER,
            'resource': [PATH_RESOURCE, PATH_RESOURCE],
        },
    )
    assert exchange.status_code == 400
    assert exchange.json()['error'] == 'invalid_request'

    code = _authorize(endpoint, client_id, PATH_RESOURCE)
    issued = _exchange(endpoint, client_id, client_secret, code, PATH_RESOURCE)
    rotated = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': [PATH_RESOURCE, PATH_RESOURCE],
        },
    )
    assert rotated.status_code == 400
    assert rotated.json()['error'] == 'invalid_request'


# === State level resource binding tests ===


def test_state_level_binding_canonicalizes_stored_root_form(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / 'state-downloads'
    downloads.mkdir(mode=0o700)
    with OAuthState(
        tmp_path / 'state2' / 'oauth.sqlite3',
        download_path=downloads,
        service_id='gmail',
        resource=ROOT_RESOURCE,
        readonly_capabilities=READONLY,
        access_token_ttl_seconds=60,
        refresh_token_ttl_seconds=6000,
    ) as state:
        state.migrate_legacy()
        issued_client = state.register_client([REDIRECT])
        client_id = issued_client.client.client_id
        code = state.issue_authorization_code(
            client_id=client_id,
            redirect_uri=REDIRECT,
            code_challenge=CHALLENGE,
            resource=ROOT_SLASHED,
        )
        issued = state.redeem_authorization_code(
            code=code,
            client_id=client_id,
            client_secret=issued_client.client_secret,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
            resource=ROOT_RESOURCE,
        )
        rotated = state.redeem_refresh_token(
            refresh_token=issued.refresh_token,
            client_id=client_id,
            resource=ROOT_SLASHED,
        )
        assert rotated.access_token
        with pytest.raises(InvalidTarget):
            state.redeem_refresh_token(
                refresh_token=rotated.refresh_token,
                client_id=client_id,
                resource=PATH_RESOURCE,
            )


def test_state_level_binding_with_path_resource(tmp_path: Path) -> None:
    downloads = tmp_path / 'path-downloads'
    downloads.mkdir(mode=0o700)
    with OAuthState(
        tmp_path / 'path-state' / 'oauth.sqlite3',
        download_path=downloads,
        service_id='gmail',
        resource=PATH_RESOURCE,
        readonly_capabilities=READONLY,
        access_token_ttl_seconds=60,
        refresh_token_ttl_seconds=6000,
    ) as state:
        state.migrate_legacy()
        issued_client = state.register_client([REDIRECT])
        client_id = issued_client.client.client_id
        code = state.issue_authorization_code(
            client_id=client_id,
            redirect_uri=REDIRECT,
            code_challenge=CHALLENGE,
            resource=PATH_RESOURCE,
        )
        issued = state.redeem_authorization_code(
            code=code,
            client_id=client_id,
            client_secret=issued_client.client_secret,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
            resource=PATH_RESOURCE,
        )
        rotated = state.redeem_refresh_token(
            refresh_token=issued.refresh_token,
            client_id=client_id,
            resource=PATH_RESOURCE,
        )
        assert rotated.access_token
        with pytest.raises(InvalidTarget):
            state.redeem_refresh_token(
                refresh_token=rotated.refresh_token,
                client_id=client_id,
                resource=PATH_SLASHED,
            )
