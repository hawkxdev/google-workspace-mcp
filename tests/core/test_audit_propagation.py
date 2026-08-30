"""Tests for fail-closed audit propagation."""

import asyncio
import base64
import hashlib
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_workspace_mcp.audit.logger import AuditError
from google_workspace_mcp.auth.bearer import BearerAuthMiddleware
from google_workspace_mcp.auth.context import current_request_context
from google_workspace_mcp.auth.oauth import OAuthEndpoints
from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport.authorization import PolicyMCPServer
from google_workspace_mcp.transport.server import build_app

ISSUER = 'https://mcp.example.test/gmail'
RESOURCE = 'https://mcp.example.test/gmail/mcp'
LOGIN_USER = 'test-user'
LOGIN_PASS = 'test-pass'
REDIRECT_URI = 'https://client.example.test/callback'
CODE_VERIFIER = 'pkce-verifier-marker-long-enough'
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
    .rstrip(b'=')
    .decode()
)


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    state_dir = tmp_path / 'state'
    state_dir.mkdir(mode=0o700, parents=True)
    return ServiceConfig(
        service_id='gmail',
        public_url=ISSUER,
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
        download_path=state_dir / 'downloads',
        oauth_state_path=state_dir / 'oauth_state.sqlite3',
        google_token_path=state_dir / 'google_token.json',
        audit_log_path=state_dir / 'audit.jsonl',
        oauth_login_username=LOGIN_USER,
        oauth_login_password=LOGIN_PASS,
        allowed_hosts=(),
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
    download_dir = tmp_path / 'downloads'
    download_dir.mkdir(mode=0o700, parents=True)
    with OAuthState(
        service_config.oauth_state_path,
        service_id='gmail',
        resource=RESOURCE,
        download_path=download_dir,
    ) as state:
        yield state


def _failing_audit_writer(record: dict[str, object]) -> None:
    raise AuditError('Failed to record audit event')


def test_oauth_refresh_failure_propagates_audit_error(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    registered = oauth_state.register_client((REDIRECT_URI,))
    client_id = registered.client.client_id
    endpoints = OAuthEndpoints(
        config=service_config,
        oauth_state=oauth_state,
        login_username=LOGIN_USER,
        login_password=LOGIN_PASS,
        audit_writer=_failing_audit_writer,
    )

    async def _run() -> None:
        scope = {
            'type': 'http',
            'method': 'POST',
            'path': '/oauth/token',
            'headers': [
                (b'host', b'mcp.example.test'),
                (b'content-type', b'application/x-www-form-urlencoded'),
            ],
        }
        body = (
            f'grant_type=refresh_token&refresh_token=invalid-token'
            f'&client_id={client_id}&resource={RESOURCE}'
        ).encode()
        request = Request(scope)

        async def _receive() -> dict[str, object]:
            return {'type': 'http.request', 'body': body, 'more_body': False}

        request._receive = _receive  # type: ignore[method-assign]
        with pytest.raises(AuditError, match='Failed to record audit event'):
            await endpoints.oauth_token(request)

    asyncio.run(_run())


def test_oauth_refresh_rotation_propagates_audit_error(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    registered = oauth_state.register_client((REDIRECT_URI,))
    client_id = registered.client.client_id
    code = oauth_state.issue_authorization_code(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        code_challenge=CODE_CHALLENGE,
        resource=RESOURCE,
    )
    issued = oauth_state.redeem_authorization_code(
        code=code,
        client_id=client_id,
        client_secret=registered.client_secret,
        redirect_uri=REDIRECT_URI,
        code_verifier=CODE_VERIFIER,
        resource=RESOURCE,
    )
    assert issued.refresh_token is not None
    endpoints = OAuthEndpoints(
        config=service_config,
        oauth_state=oauth_state,
        login_username=LOGIN_USER,
        login_password=LOGIN_PASS,
        audit_writer=_failing_audit_writer,
    )

    async def _run() -> None:
        scope = {
            'type': 'http',
            'method': 'POST',
            'path': '/oauth/token',
            'headers': [
                (b'host', b'mcp.example.test'),
                (b'content-type', b'application/x-www-form-urlencoded'),
            ],
        }
        body = (
            f'grant_type=refresh_token&refresh_token={issued.refresh_token}'
            f'&client_id={client_id}&resource={RESOURCE}'
        ).encode()
        request = Request(scope)

        async def _receive() -> dict[str, object]:
            return {'type': 'http.request', 'body': body, 'more_body': False}

        request._receive = _receive  # type: ignore[method-assign]
        with pytest.raises(AuditError, match='Failed to record audit event'):
            await endpoints.oauth_token(request)

    asyncio.run(_run())


def test_oauth_write_audit_runs_off_event_loop(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    caller_threads: list[threading.Thread] = []

    def _threaded_audit_writer(record: dict[str, object]) -> None:
        caller_threads.append(threading.current_thread())

    endpoints = OAuthEndpoints(
        config=service_config,
        oauth_state=oauth_state,
        login_username=LOGIN_USER,
        login_password=LOGIN_PASS,
        audit_writer=_threaded_audit_writer,
    )

    async def _run() -> None:
        main_thread = threading.current_thread()
        await endpoints._write_audit({'test': 'event'})
        assert len(caller_threads) == 1
        assert caller_threads[0] != main_thread

    asyncio.run(_run())


def test_principal_propagates_to_sync_handler(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    registered = oauth_state.register_client((REDIRECT_URI,))
    issued = oauth_state.issue_access_token(
        client_id=registered.client.client_id,
        resource=RESOURCE,
    )

    def sync_probe(_: Request) -> JSONResponse:
        ctx = current_request_context()
        assert ctx is not None
        return JSONResponse(
            {
                'principal_id': ctx.principal.principal_id,
                'credential_id': ctx.principal.credential_id,
                'client_id': ctx.principal.client_id,
                'policy': ctx.principal.policy,
                'capabilities': sorted(ctx.principal.capabilities),
                'full_access': ctx.principal.full_access,
                'request_id': ctx.request_id,
            }
        )

    app = Starlette(
        routes=[Route(service_config.mcp_path, sync_probe, methods=['GET'])]
    )
    app.add_middleware(
        BearerAuthMiddleware,
        config=service_config,
        oauth_state=oauth_state,
    )
    client = TestClient(app)

    response = client.get(
        service_config.mcp_path,
        headers={
            'Authorization': f'Bearer {issued.access_token}',
            'User-Agent': 'probe/1',
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['principal_id'] == f'oauth:{registered.client.client_id}'
    assert body['client_id'] == registered.client.client_id
    assert body['policy'] == 'mcp_readonly_v1'
    assert body['full_access'] is False
    assert len(body['credential_id']) == 64
    assert body['request_id']
    assert issued.access_token not in response.text

    second = client.get(
        service_config.mcp_path,
        headers={'Authorization': f'Bearer {issued.access_token}'},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body['credential_id'] == body['credential_id']
    assert second_body['request_id'] != body['request_id']


def test_context_clean_outside_request() -> None:
    assert current_request_context() is None


def test_health_is_liveness_only(
    service_config: ServiceConfig,
    oauth_state: OAuthState,
) -> None:
    server = PolicyMCPServer('gmail')
    app = build_app(service_config, oauth_state, server)
    client = TestClient(app)
    r = client.get('/health')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {'service': 'gmail', 'status': 'ok'}
    assert str(service_config.audit_log_path) not in r.text
    assert str(service_config.oauth_state_path) not in r.text
