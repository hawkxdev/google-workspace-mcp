"""OAuth endpoint integration tests."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import urllib.parse
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from google_workspace_mcp.auth.oauth import (
    OAuthEndpoints,
    authorization_server_metadata_url,
    protected_resource_metadata_url,
)
from google_workspace_mcp.auth.state import (
    MCP_READONLY_V1,
    REAUTHORIZATION_REQUIRED,
    OAuthState,
)
from google_workspace_mcp.common.config import ServiceConfig

LOGIN_USERNAME = 'test-user'
LOGIN_PASSWORD = 'test-password'
RESOURCE = 'https://mcp.example.test/gmail/mcp'
REDIRECT_URI = 'https://client.example.test/callback'
READONLY_CAPABILITIES = ('gmail_get_message', 'gmail_search')


# Shared test fixtures


@pytest.fixture
def audit_events() -> list[dict[str, object]]:
    """Capture structured audit events."""
    return []


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    """Build isolated service configuration."""
    state_dir = tmp_path / 'state'
    state_dir.mkdir(mode=0o700, parents=True)
    return ServiceConfig(
        service_id='gmail',
        public_url=RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=tmp_path / 'downloads',
        oauth_state_path=state_dir / 'oauth_state.sqlite3',
        google_token_path=state_dir / 'google_token.json',
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=tmp_path / 'legacy' / 'clients.json',
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=2592000,
    )


@pytest.fixture
def oauth_state(
    service_config: ServiceConfig,
    tmp_path: Path,
) -> Iterator[OAuthState]:
    """Build isolated OAuth state."""
    downloads = tmp_path / 'downloads'
    downloads.mkdir(mode=0o700)
    state = OAuthState(
        service_config.oauth_state_path,
        download_path=downloads,
        service_id=service_config.service_id,
        resource=service_config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
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
    audit_events: list[dict[str, object]],
) -> OAuthEndpoints:
    """Build OAuth endpoint bundle."""
    return OAuthEndpoints(
        config=service_config,
        oauth_state=oauth_state,
        login_username=LOGIN_USERNAME,
        login_password=LOGIN_PASSWORD,
        audit_writer=audit_events.append,
    )


@pytest.fixture
def client(endpoints: OAuthEndpoints) -> TestClient:
    """Build Starlette test client."""
    app = Starlette(routes=endpoints.routes)
    return TestClient(app)


# PKCE and flow helpers


def _pkce() -> tuple[str, str]:
    """Generate PKCE verifier pair."""
    verifier = 'test-pkce-verifier-that-is-long-enough'
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return verifier, challenge


def _register(
    client: TestClient, redirect_uri: str = REDIRECT_URI
) -> tuple[str, str, str]:
    """Register dynamic test client."""
    response = client.post(
        '/gmail/mcp/oauth/register',
        json={
            'client_name': 'Test Client',
            'redirect_uris': [redirect_uri],
        },
    )
    assert response.status_code == 201
    body = response.json()
    return body['client_id'], body['client_secret'], redirect_uri


def _write_pending_legacy_client(
    legacy_path: Path,
) -> tuple[str, str, str]:
    """Write unmigrated legacy client."""
    client_id = 'pending-legacy-client'
    client_secret = 'pending-legacy-secret'
    redirect_uri = 'https://pending.example.test/callback'
    legacy_path.parent.mkdir(mode=0o700, parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                client_id: {
                    'client_secret': client_secret,
                    'redirect_uris': [redirect_uri],
                    'created_at': 1.0,
                }
            }
        )
    )
    legacy_path.chmod(0o600)
    return client_id, client_secret, redirect_uri


def _authz_params(client_id: str, redirect_uri: str) -> dict[str, str]:
    """Build authorization request parameters."""
    _, challenge = _pkce()
    return {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': 'xyz',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'resource': RESOURCE,
    }


def _authorize(
    client: TestClient,
    client_id: str,
    redirect_uri: str,
) -> str:
    """Execute interactive authorization flow."""
    params = _authz_params(client_id, redirect_uri)
    response = client.post(
        '/gmail/mcp/oauth/authorize',
        data={
            **params,
            'username': LOGIN_USERNAME,
            'password': LOGIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(response.headers['location']).query
    )
    assert query['state'] == ['xyz']
    return query['code'][0]


def _exchange(
    client: TestClient,
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    verifier: str | None = None,
    resource: str = RESOURCE,
):
    """Execute token exchange request."""
    if verifier is None:
        verifier, _ = _pkce()
    return client.post(
        '/gmail/mcp/oauth/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'code_verifier': verifier,
            'resource': resource,
        },
    )


# Metadata and helper tests


def test_oauth_metadata_advertises_confidential_code_flow(client):
    response = client.get('/.well-known/oauth-authorization-server/gmail/mcp')

    assert response.status_code == 200
    body = response.json()
    assert body['issuer'] == RESOURCE
    assert body['resource'] == RESOURCE
    assert body['authorization_endpoint'] == f'{RESOURCE}/oauth/authorize'
    assert body['token_endpoint'] == f'{RESOURCE}/oauth/token'
    assert body['registration_endpoint'] == f'{RESOURCE}/oauth/register'
    assert body['response_types_supported'] == ['code']
    assert body['code_challenge_methods_supported'] == ['S256']
    assert body['token_endpoint_auth_methods_supported'] == [
        'client_secret_post'
    ]
    assert set(body['grant_types_supported']) == {
        'authorization_code',
        'refresh_token',
    }


def test_protected_resource_metadata_uses_canonical_resource(client):
    response = client.get('/.well-known/oauth-protected-resource/gmail/mcp')

    assert response.status_code == 200
    assert response.json() == {
        'resource': RESOURCE,
        'authorization_servers': [RESOURCE],
        'bearer_methods_supported': ['header'],
    }


def test_encoded_metadata_paths_reach_endpoint_handlers(
    service_config: ServiceConfig, tmp_path: Path
):
    encoded_resource = 'https://mcp.example.test/gmail%2Fmcp'
    encoded_config = replace(
        service_config,
        public_url=encoded_resource,
        oauth_state_path=tmp_path / 'encoded' / 'oauth.sqlite3',
    )
    state = OAuthState(
        encoded_config.oauth_state_path,
        download_path=tmp_path / 'encoded-downloads',
        service_id=encoded_config.service_id,
        resource=encoded_config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
    )
    try:
        endpoints = OAuthEndpoints(
            config=encoded_config,
            oauth_state=state,
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            audit_writer=lambda _: None,
        )
        encoded_client = TestClient(Starlette(routes=endpoints.routes))

        protected = encoded_client.get(
            '/.well-known/oauth-protected-resource/gmail%2Fmcp'
        )
        server = encoded_client.get(
            '/.well-known/oauth-authorization-server/gmail%2Fmcp'
        )

        assert protected.status_code == 200
        assert protected.json()['resource'] == encoded_resource
        assert server.status_code == 200
        assert server.json()['issuer'] == encoded_resource
    finally:
        state.close()


def test_protected_resource_metadata_url_inserts_well_known_before_path():
    assert protected_resource_metadata_url(RESOURCE) == (
        'https://mcp.example.test'
        '/.well-known/oauth-protected-resource/gmail/mcp'
    )
    assert protected_resource_metadata_url('https://mcp.example.test') == (
        'https://mcp.example.test/.well-known/oauth-protected-resource'
    )
    assert protected_resource_metadata_url('https://mcp.example.test/') == (
        'https://mcp.example.test/.well-known/oauth-protected-resource'
    )


def test_authorization_server_metadata_url_inserts_well_known_before_path():
    assert authorization_server_metadata_url(RESOURCE) == (
        'https://mcp.example.test'
        '/.well-known/oauth-authorization-server/gmail/mcp'
    )
    assert authorization_server_metadata_url('https://mcp.example.test') == (
        'https://mcp.example.test/.well-known/oauth-authorization-server'
    )
    assert authorization_server_metadata_url('https://mcp.example.test/') == (
        'https://mcp.example.test/.well-known/oauth-authorization-server'
    )
    assert authorization_server_metadata_url(f'{RESOURCE}/') == (
        'https://mcp.example.test'
        '/.well-known/oauth-authorization-server/gmail/mcp'
    )


@pytest.mark.parametrize(
    'invalid_url',
    [
        'http://mcp.example.test/gmail/mcp',
        'https://mcp.example.test/gmail/mcp#frag',
        'not-a-url',
        '',
    ],
)
def test_metadata_url_helpers_reject_invalid_inputs(invalid_url: str):
    with pytest.raises(ValueError):
        protected_resource_metadata_url(invalid_url)
    with pytest.raises(ValueError):
        authorization_server_metadata_url(invalid_url)


def test_metadata_url_helpers_apply_distinct_query_rules():
    resource = f'{RESOURCE}?tenant=owner'

    assert protected_resource_metadata_url(resource) == (
        'https://mcp.example.test'
        '/.well-known/oauth-protected-resource/gmail/mcp?tenant=owner'
    )
    with pytest.raises(ValueError, match='issuer'):
        authorization_server_metadata_url(resource)


# Client registration tests


def test_registration_returns_per_client_secret_and_persists(
    client, oauth_state: OAuthState
):
    client_id, client_secret, redirect_uri = _register(client)

    assert client_id.startswith('gwmcp-gmail-')
    assert len(client_secret) == 64
    assert oauth_state.client_redirect_uri_allowed(client_id, redirect_uri)
    assert oauth_state.verify_client_secret(client_id, client_secret)


@pytest.mark.parametrize(
    'redirect_uris',
    [
        [],
        [123],
        ['http://evil.example.test/callback'],
        ['https://client.example.test/callback#fragment'],
        [
            'https://safe.example.test/callback',
            'http://evil.example.test/callback',
        ],
    ],
)
def test_registration_rejects_empty_or_invalid_redirect_metadata(
    client,
    oauth_state: OAuthState,
    redirect_uris,
):
    clients_before = oauth_state.list_clients()

    response = client.post(
        '/gmail/mcp/oauth/register',
        json={'redirect_uris': redirect_uris},
    )

    assert response.status_code == 400
    assert response.json()['error'] == 'invalid_redirect_uri'
    assert oauth_state.list_clients() == clients_before


# D19 multi client regression tests


def test_two_distinct_clients_can_register_same_redirect_and_both_authorize(
    client,
):
    shared_redirect = 'https://shared.example.test/callback'
    first_id, first_secret, _ = _register(client, shared_redirect)
    second_id, second_secret, _ = _register(client, shared_redirect)

    assert first_id != second_id
    assert first_secret != second_secret

    first_code = _authorize(client, first_id, shared_redirect)
    second_code = _authorize(client, second_id, shared_redirect)

    first = _exchange(
        client,
        code=first_code,
        client_id=first_id,
        client_secret=first_secret,
        redirect_uri=shared_redirect,
    )
    second = _exchange(
        client,
        code=second_code,
        client_id=second_id,
        client_secret=second_secret,
        redirect_uri=shared_redirect,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()['access_token'] != second.json()['access_token']


def test_two_dynamic_clients_receive_independent_access_tokens(client):
    first_id, first_secret, first_redirect = _register(client)
    second_id, second_secret, second_redirect = _register(
        client, 'https://second.example.test/callback'
    )
    first_code = _authorize(client, first_id, first_redirect)
    second_code = _authorize(client, second_id, second_redirect)

    first = _exchange(
        client,
        code=first_code,
        client_id=first_id,
        client_secret=first_secret,
        redirect_uri=first_redirect,
    )
    second = _exchange(
        client,
        code=second_code,
        client_id=second_id,
        client_secret=second_secret,
        redirect_uri=second_redirect,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_token = first.json()['access_token']
    second_token = second.json()['access_token']
    assert first_token != second_token
    assert first.json()['expires_in'] == 3600
    first_refresh = first.json()['refresh_token']
    second_refresh = second.json()['refresh_token']
    assert first_refresh != second_refresh
    assert first_refresh not in {first_token, second_token}


# Authorization and exchange flow tests


def test_authorization_code_survives_state_reopen(
    client, service_config: ServiceConfig, tmp_path: Path
):
    client_id, client_secret, redirect_uri = _register(client)
    code = _authorize(client, client_id, redirect_uri)

    reopened_state = OAuthState(
        service_config.oauth_state_path,
        download_path=tmp_path / 'downloads',
        service_id=service_config.service_id,
        resource=service_config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
        legacy_path=service_config.legacy_clients_path,
        approved_legacy_client_ids=service_config.approved_legacy_client_ids,
        access_token_ttl_seconds=service_config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=service_config.refresh_token_ttl_seconds,
    )
    try:
        endpoints = OAuthEndpoints(
            config=service_config,
            oauth_state=reopened_state,
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            audit_writer=lambda _: None,
        )
        reopened_client = TestClient(Starlette(routes=endpoints.routes))
        response = _exchange(
            reopened_client,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )

        assert response.status_code == 200
        assert response.json()['access_token'].startswith('v1.')
    finally:
        reopened_state.close()


def test_wrong_client_secret_does_not_consume_code(client):
    client_id, client_secret, redirect_uri = _register(client)
    code = _authorize(client, client_id, redirect_uri)

    denied = _exchange(
        client,
        code=code,
        client_id=client_id,
        client_secret='wrong-secret',
        redirect_uri=redirect_uri,
    )
    accepted = _exchange(
        client,
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )

    assert denied.status_code == 401
    assert denied.json()['error'] == 'invalid_client'
    assert accepted.status_code == 200


def test_invalid_exchange_inputs_do_not_consume_code(client):
    client_id, client_secret, redirect_uri = _register(client)
    code = _authorize(client, client_id, redirect_uri)

    bad_pkce = _exchange(
        client,
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        verifier='wrong-pkce-verifier',
    )
    bad_resource = _exchange(
        client,
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        resource='https://mcp.example.test/drive/mcp',
    )
    accepted = _exchange(
        client,
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )

    assert bad_pkce.status_code == 400
    assert bad_pkce.json()['error'] == 'invalid_grant'
    assert bad_resource.status_code == 400
    assert bad_resource.json()['error'] == 'invalid_target'
    assert accepted.status_code == 200


# Login and consent security tests


def test_authorize_requires_interactive_login(client):
    client_id, _, redirect_uri = _register(client)
    params = _authz_params(client_id, redirect_uri)

    get_response = client.get('/gmail/mcp/oauth/authorize', params=params)
    wrong_response = client.post(
        '/gmail/mcp/oauth/authorize',
        data={**params, 'username': LOGIN_USERNAME, 'password': 'wrong'},
        follow_redirects=False,
    )

    assert get_response.status_code == 200
    assert '<form' in get_response.text
    assert wrong_response.status_code == 401
    assert 'location' not in wrong_response.headers


def test_authorize_rejects_wrong_unicode_credentials_without_server_error(
    client,
):
    client_id, _, redirect_uri = _register(client)
    params = _authz_params(client_id, redirect_uri)
    with TestClient(
        client.app, raise_server_exceptions=False
    ) as tolerant_client:
        response = tolerant_client.post(
            '/gmail/mcp/oauth/authorize',
            data={**params, 'username': 'пользователь', 'password': 'пароль'},
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert 'Invalid username or password' in response.text


def test_consent_form_identifies_client_and_redirect_without_rendering_markup(
    client,
):
    redirect_uri = (
        'https://client.example.test/callback?next=<unsafe>&mode=consent'
    )
    registration = client.post(
        '/gmail/mcp/oauth/register',
        json={
            'client_name': '<img src=x onerror=alert(1)>',
            'redirect_uris': [redirect_uri],
        },
    )
    assert registration.status_code == 201

    response = client.get(
        '/gmail/mcp/oauth/authorize',
        params=_authz_params(registration.json()['client_id'], redirect_uri),
    )

    assert response.status_code == 200
    assert '<img src=x' not in response.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in response.text
    assert (
        '<code>https://client.example.test/callback?'
        'next=&lt;unsafe&gt;&amp;mode=consent</code>'
    ) in response.text


@pytest.mark.parametrize(
    ('login_username', 'login_password'),
    [(LOGIN_USERNAME, ''), ('', LOGIN_PASSWORD)],
)
def test_missing_login_configuration_fails_closed(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    login_username: str,
    login_password: str,
):
    endpoints = OAuthEndpoints(
        config=service_config,
        oauth_state=oauth_state,
        login_username=login_username,
        login_password=login_password,
        audit_writer=lambda _: None,
    )
    misconfigured_client = TestClient(Starlette(routes=endpoints.routes))
    client_id, _, redirect_uri = _register(misconfigured_client)
    params = _authz_params(client_id, redirect_uri)

    get_response = misconfigured_client.get(
        '/gmail/mcp/oauth/authorize', params=params, follow_redirects=False
    )
    post_response = misconfigured_client.post(
        '/gmail/mcp/oauth/authorize',
        data={**params, 'username': LOGIN_USERNAME, 'password': 'anything'},
        follow_redirects=False,
    )

    assert get_response.status_code == 503
    assert post_response.status_code == 503
    assert 'location' not in get_response.headers
    assert 'location' not in post_response.headers


# Legacy import and validation tests


def test_pending_import_requires_fresh_login_then_issues_exact_readonly_token(
    tmp_path: Path,
):
    # 1. Build legacy config
    legacy_path = tmp_path / 'legacy_pending' / 'clients.json'
    client_id, client_secret, redirect_uri = _write_pending_legacy_client(
        legacy_path
    )
    state_path = tmp_path / 'pending_state' / 'oauth_state.sqlite3'
    state_path.parent.mkdir(mode=0o700, parents=True)
    config = ServiceConfig(
        service_id='gmail',
        public_url=RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=tmp_path / 'pending_downloads',
        oauth_state_path=state_path,
        google_token_path=state_path.parent / 'google_token.json',
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=legacy_path,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=2592000,
    )
    downloads = tmp_path / 'pending_downloads'
    downloads.mkdir(mode=0o700)
    # 2. Open OAuth endpoints
    with OAuthState(
        config.oauth_state_path,
        download_path=downloads,
        service_id=config.service_id,
        resource=config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
        legacy_path=config.legacy_clients_path,
        approved_legacy_client_ids=config.approved_legacy_client_ids,
        access_token_ttl_seconds=config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=config.refresh_token_ttl_seconds,
    ) as state:
        state.migrate_legacy()
        endpoints = OAuthEndpoints(
            config=config,
            oauth_state=state,
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            audit_writer=lambda _: None,
        )
        test_client = TestClient(Starlette(routes=endpoints.routes))
        params = _authz_params(client_id, redirect_uri)

        # 3. Reject stale login
        denied = test_client.post(
            '/gmail/mcp/oauth/authorize',
            data={**params, 'username': LOGIN_USERNAME, 'password': 'wrong'},
            follow_redirects=False,
        )
        assert denied.status_code == 401
        assert state.get_client(client_id).policy == REAUTHORIZATION_REQUIRED

        # 4. Complete reauthorization
        code = _authorize(test_client, client_id, redirect_uri)
        accepted = _exchange(
            test_client,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )

        # 5. Verify readonly token
        assert accepted.status_code == 200
        assert state.get_client(client_id).policy == MCP_READONLY_V1
        token_id = accepted.json()['access_token'].split('.')[1]
        token = next(
            item for item in state.list_tokens() if item.token_id == token_id
        )
        assert token.policy == MCP_READONLY_V1
        assert token.capabilities == READONLY_CAPABILITIES


def test_reauthorization_rolls_back_if_code_insert_fails(
    tmp_path: Path,
):
    # 1. Build rollback config
    legacy_path = tmp_path / 'legacy_rollback' / 'clients.json'
    client_id, _, redirect_uri = _write_pending_legacy_client(legacy_path)
    state_path = tmp_path / 'rollback_state' / 'oauth_state.sqlite3'
    state_path.parent.mkdir(mode=0o700, parents=True)
    config = ServiceConfig(
        service_id='gmail',
        public_url=RESOURCE,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=tmp_path / 'rollback_downloads',
        oauth_state_path=state_path,
        google_token_path=state_path.parent / 'google_token.json',
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=legacy_path,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=2592000,
    )
    downloads = tmp_path / 'rollback_downloads'
    downloads.mkdir(mode=0o700)
    # 2. Open rollback state
    with OAuthState(
        config.oauth_state_path,
        download_path=downloads,
        service_id=config.service_id,
        resource=config.public_url,
        readonly_capabilities=READONLY_CAPABILITIES,
        legacy_path=config.legacy_clients_path,
        approved_legacy_client_ids=config.approved_legacy_client_ids,
        access_token_ttl_seconds=config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=config.refresh_token_ttl_seconds,
    ) as state:
        state.migrate_legacy()
        # 3. Install failing trigger
        state._connection.execute(
            'CREATE TRIGGER fail_code_insert BEFORE INSERT ON '
            'authorization_codes BEGIN '
            "SELECT RAISE(ABORT, 'synthetic code insert failure'); END"
        )
        endpoints = OAuthEndpoints(
            config=config,
            oauth_state=state,
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            audit_writer=lambda _: None,
        )
        test_client = TestClient(Starlette(routes=endpoints.routes))

        # 4. Exercise failed authorization
        with pytest.raises(
            sqlite3.IntegrityError, match='synthetic code insert failure'
        ):
            test_client.post(
                '/gmail/mcp/oauth/authorize',
                data={
                    **_authz_params(client_id, redirect_uri),
                    'username': LOGIN_USERNAME,
                    'password': LOGIN_PASSWORD,
                },
                follow_redirects=False,
            )

        # 5. Verify transaction rollback
        assert state.get_client(client_id).policy == REAUTHORIZATION_REQUIRED


@pytest.mark.parametrize(
    'changes,error',
    [
        ({'response_type': 'token'}, 'unsupported_response_type'),
        ({'client_id': 'unknown-client'}, 'invalid_client'),
        (
            {'redirect_uri': 'https://evil.example.test/callback'},
            'invalid_request',
        ),
        ({'code_challenge': ''}, 'invalid_request'),
        ({'code_challenge_method': 'plain'}, 'invalid_request'),
        ({'resource': 'https://mcp.example.test/drive/mcp'}, 'invalid_target'),
    ],
)
def test_authorization_request_validation(client, changes, error):
    client_id, _, redirect_uri = _register(client)
    params = _authz_params(client_id, redirect_uri)
    params.update(changes)

    response = client.get('/gmail/mcp/oauth/authorize', params=params)

    assert response.status_code == 400
    assert response.json()['error'] == error
