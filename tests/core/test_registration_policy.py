"""Dynamic client registration policy tests."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from google_workspace_mcp.auth.oauth import OAuthEndpoints
from google_workspace_mcp.auth.state import (
    LEGACY_FULL,
    MCP_READONLY_V1,
    REAUTHORIZATION_REQUIRED,
    OAuthState,
)
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport import create_service_app

ISSUER = 'https://mcp.example.test/gmail'
RESOURCE = 'https://mcp.example.test/gmail/mcp'
REDIRECT = 'https://client.example.test/callback'
READONLY = ('gmail_get_message', 'gmail_search')
LOGIN_USERNAME = 'policy-user'
LOGIN_PASSWORD = 'policy-password'
VERIFIER = 'registration-policy-verifier-that-is-long-enough'
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b'=')
    .decode()
)

# === Shared builders ===


def _config(root: Path, policy: str) -> ServiceConfig:
    """Build isolated service configuration."""
    state_dir = root / 'state'
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ServiceConfig(
        service_id='gmail',
        public_url=ISSUER,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=downloads,
        oauth_state_path=state_dir / 'oauth_state.sqlite3',
        google_token_path=state_dir / 'google_token.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username=LOGIN_USERNAME,
        oauth_login_password=LOGIN_PASSWORD,
        allowed_hosts=('mcp.example.test',),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=2592000,
        dcr_default_policy=policy,
    )


def _state(root: Path, policy: str) -> OAuthState:
    """Open OAuth state with an explicit registration policy."""
    config = _config(root, policy)
    return OAuthState(
        config.oauth_state_path,
        download_path=config.download_path,
        service_id=config.service_id,
        resource=config.resource_url,
        readonly_capabilities=READONLY,
        registration_policy=policy,
    )


# === State registration policy ===


@pytest.mark.parametrize(
    ('policy', 'capabilities'),
    [(MCP_READONLY_V1, READONLY), (LEGACY_FULL, ())],
)
def test_new_registration_receives_configured_policy(
    tmp_path: Path, policy: str, capabilities: tuple[str, ...]
) -> None:
    state = _state(tmp_path, policy)
    try:
        registered = state.register_client([REDIRECT])
        client = state.get_client(registered.client.client_id)

        assert client is not None
        assert client.policy == policy
        assert client.capabilities == capabilities
    finally:
        state.close()


@pytest.mark.parametrize('policy', [REAUTHORIZATION_REQUIRED, 'unknown'])
def test_registration_policy_outside_readonly_and_full_is_rejected(
    tmp_path: Path, policy: str
) -> None:
    with pytest.raises(ValueError, match='registration_policy'):
        _state(tmp_path, policy)


def test_changing_registration_policy_keeps_existing_clients(
    tmp_path: Path,
) -> None:
    readonly_state = _state(tmp_path, MCP_READONLY_V1)
    try:
        earlier = readonly_state.register_client([REDIRECT]).client.client_id
    finally:
        readonly_state.close()

    full_state = _state(tmp_path, LEGACY_FULL)
    try:
        later = full_state.register_client([REDIRECT]).client.client_id
        earlier_client = full_state.get_client(earlier)
        later_client = full_state.get_client(later)

        assert earlier_client is not None
        assert later_client is not None
        assert earlier_client.policy == MCP_READONLY_V1
        assert later_client.policy == LEGACY_FULL
    finally:
        full_state.close()


# === HTTP registration flow ===


def test_http_registration_under_full_policy_issues_full_token(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, LEGACY_FULL)
    state = _state(tmp_path, LEGACY_FULL)
    try:
        endpoints = OAuthEndpoints(
            config=config,
            oauth_state=state,
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            audit_writer=lambda _: None,
        )
        client = TestClient(Starlette(routes=endpoints.routes))

        registration = client.post(
            '/gmail/oauth/register',
            json={'client_name': 'Policy Client', 'redirect_uris': [REDIRECT]},
        )
        assert registration.status_code == 201
        client_id = registration.json()['client_id']
        client_secret = registration.json()['client_secret']
        authorization = client.post(
            '/gmail/oauth/authorize',
            data={
                'response_type': 'code',
                'client_id': client_id,
                'redirect_uri': REDIRECT,
                'state': 'policy-state',
                'code_challenge': CHALLENGE,
                'code_challenge_method': 'S256',
                'resource': RESOURCE,
                'username': LOGIN_USERNAME,
                'password': LOGIN_PASSWORD,
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 302
        code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(authorization.headers['location']).query
        )['code'][0]
        exchange = client.post(
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
        assert exchange.status_code == 200
        token = state.lookup_access_token(exchange.json()['access_token'])
        stored = state.get_client(client_id)

        assert stored is not None
        assert stored.policy == LEGACY_FULL
        assert token is not None
        assert token.policy == LEGACY_FULL
    finally:
        state.close()


# === Factory wiring ===


@pytest.mark.parametrize('policy', [MCP_READONLY_V1, LEGACY_FULL])
def test_service_factory_passes_configured_registration_policy(
    tmp_path: Path, policy: str
) -> None:
    _, _, state = create_service_app(_config(tmp_path, policy))
    try:
        registered = state.register_client([REDIRECT])

        assert registered.client.policy == policy
    finally:
        state.close()
