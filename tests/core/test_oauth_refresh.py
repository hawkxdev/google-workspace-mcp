"""Refresh token rotation contracts."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from google_workspace_mcp.auth.oauth import OAuthEndpoints
from google_workspace_mcp.auth.state import (
    MCP_READONLY_V1,
    InvalidClient,
    InvalidGrant,
    InvalidTarget,
    OAuthState,
)
from google_workspace_mcp.common.config import ServiceConfig

LOGIN_USERNAME = 'test-user'
LOGIN_PASSWORD = 'test-pass'
ISSUER = 'https://mcp.example.test/gmail'
RESOURCE = 'https://mcp.example.test/gmail/mcp'
REDIRECT = 'https://client.example.test/callback'
READONLY = ('gmail_get_message', 'gmail_search')
VERIFIER = 'pkce-verifier-marker-long-enough'
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b'=')
    .decode()
)


# Shared state fixtures


@pytest.fixture
def state_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create isolated OAuth paths."""
    downloads = tmp_path / 'downloads'
    downloads.mkdir(mode=0o700)
    state = tmp_path / 'state' / 'oauth_state.sqlite3'
    legacy = tmp_path / 'legacy.json'
    return downloads, state, legacy


def _state(
    state_paths: tuple[Path, Path, Path],
    *,
    now: list[float] | None = None,
) -> OAuthState:
    """Build configured OAuth state."""
    downloads, state_path, legacy_path = state_paths
    state = OAuthState(
        state_path,
        download_path=downloads,
        service_id='gmail',
        resource=RESOURCE,
        readonly_capabilities=READONLY,
        legacy_path=legacy_path,
        access_token_ttl_seconds=60,
        refresh_token_ttl_seconds=6000,
        clock=(lambda: now[0]) if now is not None else None,
    )
    state.migrate_legacy()
    return state


def _authorize(state: OAuthState) -> tuple[str, str, object]:
    """Complete authorization code exchange."""
    issued_client = state.register_client([REDIRECT])
    client_id = issued_client.client.client_id
    code = state.issue_authorization_code(
        client_id=client_id,
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        resource=RESOURCE,
    )
    issued = state.redeem_authorization_code(
        code=code,
        client_id=client_id,
        client_secret=issued_client.client_secret,
        redirect_uri=REDIRECT,
        code_verifier=VERIFIER,
        resource=RESOURCE,
    )
    return client_id, issued_client.client_secret, issued


# State lifecycle tests


def test_authorization_code_exchange_returns_a_refresh_token(
    state_paths,
) -> None:
    with _state(state_paths) as state:
        _, _, issued = _authorize(state)
        assert issued.refresh_token
        assert issued.refresh_token != issued.access_token


def test_refresh_rotates_and_preserves_policy(state_paths) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
        rotated = state.redeem_refresh_token(
            refresh_token=issued.refresh_token,
            client_id=client_id,
            resource=RESOURCE,
        )
        assert rotated.access_token != issued.access_token
        assert rotated.refresh_token
        assert rotated.refresh_token != issued.refresh_token
        assert rotated.token.policy == MCP_READONLY_V1
        assert rotated.token.capabilities == READONLY
        assert rotated.token.resource == RESOURCE
        assert state.lookup_access_token(rotated.access_token) is not None


def test_refresh_needs_no_client_secret(state_paths) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
        rotated = state.redeem_refresh_token(
            refresh_token=issued.refresh_token,
            client_id=client_id,
            resource=RESOURCE,
        )
        assert rotated.access_token


def test_replayed_refresh_is_rejected_and_kills_the_family(
    state_paths,
) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
        rotated = state.redeem_refresh_token(
            refresh_token=issued.refresh_token,
            client_id=client_id,
            resource=RESOURCE,
        )
        with pytest.raises(InvalidGrant):
            state.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=client_id,
                resource=RESOURCE,
            )
        with pytest.raises(InvalidGrant):
            state.redeem_refresh_token(
                refresh_token=rotated.refresh_token,
                client_id=client_id,
                resource=RESOURCE,
            )
        assert state.lookup_access_token(rotated.access_token) is None


def test_refresh_after_client_revoke_is_rejected(state_paths) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
        assert state.revoke_client(client_id) is True
        with pytest.raises((InvalidClient, InvalidGrant)):
            state.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=client_id,
                resource=RESOURCE,
            )


def test_revoked_client_alone_stops_rotation(state_paths) -> None:
    _, state_path, _ = state_paths
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
    connection = sqlite3.connect(state_path)
    connection.execute(
        'UPDATE clients SET revoked_at = 1 WHERE client_id = ?',
        (client_id,),
    )
    connection.commit()
    connection.close()
    with _state(state_paths) as reopened:
        assert reopened.list_refresh_tokens()[0].revoked_at is None
        with pytest.raises(InvalidClient):
            reopened.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=client_id,
                resource=RESOURCE,
            )


def test_refresh_with_foreign_client_id_is_rejected(state_paths) -> None:
    with _state(state_paths) as state:
        _, _, issued = _authorize(state)
        other_id, _, _ = _authorize(state)
        with pytest.raises(InvalidGrant):
            state.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=other_id,
                resource=RESOURCE,
            )


def test_refresh_with_mismatched_resource_is_rejected(state_paths) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
        with pytest.raises(InvalidTarget):
            state.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=client_id,
                resource='https://mcp.example.test/drive/mcp',
            )


def test_expired_refresh_is_rejected(state_paths) -> None:
    now = [1000.0]
    with _state(state_paths, now=now) as state:
        client_id, _, issued = _authorize(state)
        now[0] += 6001
        with pytest.raises(InvalidGrant):
            state.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=client_id,
                resource=RESOURCE,
            )


def test_refresh_survives_restart(state_paths) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
    with _state(state_paths) as reopened:
        rotated = reopened.redeem_refresh_token(
            refresh_token=issued.refresh_token,
            client_id=client_id,
            resource=RESOURCE,
        )
        assert rotated.access_token
        assert reopened.lookup_access_token(rotated.access_token) is not None


def test_refresh_can_be_revoked_independently_of_access(
    state_paths,
) -> None:
    with _state(state_paths) as state:
        client_id, _, issued = _authorize(state)
        records = state.list_refresh_tokens()
        assert len(records) == 1
        assert state.revoke_refresh_token(records[0].refresh_id) is True
        assert state.lookup_access_token(issued.access_token) is not None
        with pytest.raises(InvalidGrant, match='revoked'):
            state.redeem_refresh_token(
                refresh_token=issued.refresh_token,
                client_id=client_id,
                resource=RESOURCE,
            )


def test_raw_refresh_secret_never_persists(state_paths) -> None:
    _, state_path, _ = state_paths
    with _state(state_paths) as state:
        _, _, issued = _authorize(state)
        secret = issued.refresh_token.split('.')[-1]
    blob = state_path.read_bytes()
    for suffix in ('-wal', '-shm'):
        sidecar = Path(f'{state_path}{suffix}')
        if sidecar.exists():
            blob += sidecar.read_bytes()
    assert secret.encode() not in blob


# Endpoint contracts


@pytest.fixture
def audit_events() -> list[dict[str, object]]:
    """Capture structured audit events."""
    return []


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    """Build isolated endpoint configuration."""
    state_dir = tmp_path / 'ep-state'
    state_dir.mkdir(mode=0o700, parents=True)
    return ServiceConfig(
        service_id='gmail',
        public_url=ISSUER,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=state_dir / 'downloads',
        oauth_state_path=state_dir / 'oauth.sqlite3',
        google_token_path=state_dir / 'google.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username=LOGIN_USERNAME,
        oauth_login_password=LOGIN_PASSWORD,
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=tmp_path / 'ep-legacy.json',
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
    downloads = tmp_path / 'ep-downloads'
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
def endpoint(endpoints: OAuthEndpoints) -> TestClient:
    """Build Starlette test client."""
    app = Starlette(routes=endpoints.routes)
    return TestClient(app)


def _endpoint_authorize(endpoint: TestClient) -> tuple[str, dict]:
    """Authorize endpoint test client."""
    registered = endpoint.post(
        '/gmail/oauth/register',
        json={'client_name': 'Headless', 'redirect_uris': [REDIRECT]},
    )
    assert registered.status_code == 201
    client_id = registered.json()['client_id']
    client_secret = registered.json()['client_secret']
    authorized = endpoint.post(
        '/gmail/oauth/authorize',
        data={
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': RESOURCE,
            'username': LOGIN_USERNAME,
            'password': LOGIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert authorized.status_code == 302
    code = authorized.headers['location'].split('code=')[1].split('&')[0]
    token = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': REDIRECT,
            'code_verifier': VERIFIER,
            'resource': RESOURCE,
        },
    )
    assert token.status_code == 200
    return client_id, token.json()


# Endpoint flow tests


def test_metadata_advertises_refresh_token_grant(
    endpoint: TestClient,
) -> None:
    payload = endpoint.get(
        '/.well-known/oauth-authorization-server/gmail'
    ).json()
    assert 'refresh_token' in payload['grant_types_supported']


def test_token_endpoint_rotates_without_client_secret(
    endpoint: TestClient,
) -> None:
    client_id, issued = _endpoint_authorize(endpoint)
    assert issued['refresh_token']
    rotated = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': RESOURCE,
        },
    )
    assert rotated.status_code == 200
    body = rotated.json()
    assert body['access_token'] != issued['access_token']
    assert body['refresh_token'] != issued['refresh_token']
    assert body['token_type'] == 'bearer'
    assert body['expires_in'] > 0


def test_token_endpoint_rejects_mismatched_resource(
    endpoint: TestClient,
) -> None:
    client_id, issued = _endpoint_authorize(endpoint)
    rotated = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': 'https://mcp.example.test/drive/mcp',
        },
    )
    assert rotated.status_code == 400
    assert rotated.json()['error'] == 'invalid_target'


def test_endpoints_reject_resource_mismatch_at_construction(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
    audit_events: list[dict[str, object]],
) -> None:
    moved_config = ServiceConfig(
        service_id=service_config.service_id,
        public_url='https://moved.example.test/gmail',
        mcp_path=service_config.mcp_path,
        host=service_config.host,
        port=service_config.port,
        download_path=service_config.download_path,
        oauth_state_path=service_config.oauth_state_path,
        google_token_path=service_config.google_token_path,
        audit_log_path=service_config.audit_log_path,
        oauth_login_username=service_config.oauth_login_username,
        oauth_login_password=service_config.oauth_login_password,
        allowed_hosts=('moved.example.test',),
        forwarded_allow_ips=service_config.forwarded_allow_ips,
        legacy_clients_path=service_config.legacy_clients_path,
        approved_legacy_client_ids=(service_config.approved_legacy_client_ids),
        access_token_ttl_seconds=(service_config.access_token_ttl_seconds),
        refresh_token_ttl_seconds=(service_config.refresh_token_ttl_seconds),
    )

    with pytest.raises(ValueError, match='resource'):
        OAuthEndpoints(
            config=moved_config,
            oauth_state=oauth_state,
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            audit_writer=audit_events.append,
        )


def test_token_endpoint_rejects_replayed_refresh(
    endpoint: TestClient,
) -> None:
    client_id, issued = _endpoint_authorize(endpoint)
    first = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': RESOURCE,
        },
    )
    assert first.status_code == 200
    replay = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': RESOURCE,
        },
    )
    assert replay.status_code == 400
    assert replay.json()['error'] == 'invalid_grant'


def test_rotation_is_audited_without_raw_credentials(
    endpoint: TestClient,
    audit_events: list[dict[str, object]],
) -> None:
    client_id, issued = _endpoint_authorize(endpoint)
    rotated = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': RESOURCE,
        },
    )
    assert rotated.status_code == 200
    rotations = [
        event
        for event in audit_events
        if event.get('operation') == 'oauth_refresh_rotation'
    ]
    assert len(rotations) == 1
    record = rotations[0]
    assert record['client_id'] == client_id
    assert record['principal_id'] == f'oauth:{client_id}'
    assert record['auth_policy'] == MCP_READONLY_V1
    assert record['operation_status'] == 'success'
    assert record['token_id_hash']
    dumped = str(audit_events)
    assert rotated.json()['access_token'] not in dumped
    assert rotated.json()['refresh_token'] not in dumped
    assert issued['refresh_token'] not in dumped


def test_refused_rotation_is_audited_too(
    endpoint: TestClient,
    audit_events: list[dict[str, object]],
) -> None:
    client_id, issued = _endpoint_authorize(endpoint)
    refused = endpoint.post(
        '/gmail/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': issued['refresh_token'],
            'client_id': client_id,
            'resource': 'https://mcp.example.test/drive/mcp',
        },
    )
    assert refused.status_code == 400
    failures = [
        event
        for event in audit_events
        if event.get('operation') == 'oauth_refresh_rotation'
        and event.get('operation_status') == 'failure'
    ]
    assert len(failures) == 1
    assert failures[0]['error'] == 'invalid_target'
    assert failures[0]['client_id'] == client_id
    assert issued['refresh_token'] not in str(audit_events)


def test_refresh_state_travels_with_the_state_backup(
    endpoint: TestClient,
    oauth_state: OAuthState,
    tmp_path: Path,
) -> None:
    client_id, issued = _endpoint_authorize(endpoint)
    destination = tmp_path / 'backup' / 'oauth-backup.sqlite3'
    destination.parent.mkdir(mode=0o700, parents=True)
    oauth_state.backup(destination)
    connection = sqlite3.connect(destination)
    try:
        rows = connection.execute(
            'SELECT client_id FROM refresh_tokens'
        ).fetchall()
        blob = destination.read_bytes()
    finally:
        connection.close()
    assert [row[0] for row in rows] == [client_id]
    assert issued['refresh_token'].split('.')[-1].encode() not in blob
