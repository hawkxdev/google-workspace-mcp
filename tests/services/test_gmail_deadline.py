"""Test bounded Gmail reads."""

import asyncio
import fcntl
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
from google.auth.exceptions import TransportError

from google_workspace_mcp.auth import context
from google_workspace_mcp.common.config import GmailReadBudget, ServiceConfig
from google_workspace_mcp.common.deadline import (
    OperationDeadline,
    OperationTimeout,
    bind_deadline,
)
from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.google_auth.store import DeadlineRequest
from google_workspace_mcp.services.gmail.client import GmailGateway
from google_workspace_mcp.services.gmail.errors import GmailProviderError
from google_workspace_mcp.services.gmail.tools.common import run_gateway

from .conftest import FakeCredentialStore


@pytest.mark.asyncio
async def test_credential_delay_returns_scrubbed_timeout() -> None:
    entered = threading.Event()
    release = threading.Event()

    def delayed() -> None:
        """Hold synthetic credential operation."""
        entered.set()
        release.wait(1)

    budget = GmailReadBudget(
        total_seconds=0.02, attempt_seconds=0.01, backoff_seconds=0.001
    )
    try:
        with pytest.raises(GmailProviderError, match='phase=credentials'):
            await run_gateway(delayed, read_budget=budget)
        assert entered.is_set()
    finally:
        release.set()


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf')])
def test_rejects_invalid_read_budget(value: float) -> None:
    with pytest.raises(ValueError):
        GmailReadBudget(total_seconds=value)


def test_rejects_attempt_larger_than_total_budget() -> None:
    with pytest.raises(ValueError):
        GmailReadBudget(total_seconds=5, attempt_seconds=10)


class Clock:
    """Advance synthetic monotonic time."""

    def __init__(self) -> None:
        """Initialize synthetic clock."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return synthetic clock reading."""
        return self.now

    def wait(self, seconds: float) -> bool:
        """Advance without real sleep."""
        self.now += seconds
        return False


@pytest.mark.parametrize('status', [200, 403, 429, 500, 'timeout'])
def test_real_sdk_retries_are_bounded_and_scrubbed(
    status: int | str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    deadline = OperationDeadline(5, 2, clock=clock)
    monkeypatch.setattr(deadline.cancelled, 'wait', clock.wait)
    calls: list[dict[str, Any]] = []

    def request(*args: Any, **kwargs: Any) -> requests.Response:
        """Return bounded provider response."""
        calls.append(kwargs)
        assert kwargs['timeout'] <= 2
        assert kwargs['timeout'] <= 5 - clock.now
        assert kwargs['allow_redirects'] is False
        clock.now += 2
        if status == 'timeout':
            raise requests.Timeout('query=user@example.com&secret=token')
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(
            {'labels': []}
            if status == 200
            else {'error': {'errors': [{'reason': 'user@example.com secret'}]}}
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, 'request', request)
    gateway = GmailGateway(FakeCredentialStore())
    with caplog.at_level(logging.DEBUG), bind_deadline(deadline):
        if status == 200:
            assert gateway.list_labels().items == ()
        elif status == 403:
            with pytest.raises(GmailProviderError, match='forbidden'):
                gateway.list_labels()
        else:
            with pytest.raises(OperationTimeout, match='phase=backoff'):
                gateway.list_labels()
    assert len(calls) == (1 if status in (200, 403) else 2)
    assert 'phase=provider' in caplog.text
    assert 'user@example.com' not in caplog.text
    assert 'secret' not in caplog.text
    for cleanup in deadline.cleanups:
        cleanup()


def test_provider_diagnostics_outside_reads_are_retained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger('googleapiclient.http')
    with caplog.at_level(logging.WARNING):
        logger.warning('known control outside a Gmail read')
        with bind_deadline(OperationDeadline(5, 1)):
            logger.warning('sensitive query inside a Gmail read')
    assert 'known control' in caplog.text
    assert 'sensitive query' not in caplog.text


def test_credential_transport_checks_remaining_time_before_every_call() -> (
    None
):
    clock = Clock()
    deadline = OperationDeadline(5, 2, clock=clock)
    timeouts: list[float] = []

    def request(*args: Any, **kwargs: Any) -> str:
        """Record bounded credential timeout."""
        timeouts.append(kwargs['timeout'])
        clock.now += kwargs['timeout']
        return 'response'

    bounded = DeadlineRequest(request, deadline)
    assert bounded(url='https://example.invalid') == 'response'
    assert bounded(url='https://example.invalid') == 'response'
    with pytest.raises(OperationTimeout, match='phase=credentials'):
        bounded(url='https://example.invalid')
    assert timeouts == [2, 2, 1]
    with pytest.raises(OperationTimeout):
        bounded(url='https://example.invalid')
    assert len(timeouts) == 3


def test_credential_error_at_expiry_retains_timeout_phase() -> None:
    clock = Clock()
    deadline = OperationDeadline(2, 2, clock=clock)

    def request(*args: Any, **kwargs: Any) -> None:
        """Fail at synthetic attempt expiry."""
        clock.now += kwargs['timeout']
        raise TransportError('sensitive provider details')

    bounded = DeadlineRequest(request, deadline)
    with pytest.raises(OperationTimeout, match='phase=credentials') as error:
        bounded(url='https://example.invalid')
    assert 'sensitive' not in str(error.value)


def test_store_stops_refresh_retry_when_backoff_cannot_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GoogleCredentialStore(tmp_path / 'credentials' / 'google.json')
    original = GoogleCredentials(
        token='old-token',
        refresh_token='refresh-token',
        client_id='client',
        client_secret='secret',
        expiry=datetime(2000, 1, 1, tzinfo=UTC),
    )
    store.save(original)
    clock = Clock()
    deadline = OperationDeadline(5, 2, clock=clock)
    monkeypatch.setattr(deadline.cancelled, 'wait', clock.wait)
    calls: list[float] = []

    def request(*args: Any, **kwargs: Any) -> None:
        """Consume synthetic credential attempt."""
        calls.append(kwargs['timeout'])
        clock.now += kwargs['timeout']
        raise TransportError('query and token must not be relayed')

    with bind_deadline(deadline), pytest.raises(OperationTimeout):
        store.refresh(request)
    assert calls == [2, 2]
    assert store.load() == original


@pytest.mark.asyncio
async def test_credential_file_lock_wait_is_bounded(tmp_path: Path) -> None:
    store = GoogleCredentialStore(tmp_path / 'credentials' / 'google.json')
    store.save(GoogleCredentials(token='token'))
    lock_path = store.path.with_name(store.path.name + '.lock')
    with lock_path.open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            with pytest.raises(GmailProviderError, match='phase=credentials'):
                await run_gateway(
                    store.refresh,
                    read_budget=GmailReadBudget(0.02, 0.01, 0.001),
                )
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)


@pytest.mark.asyncio
async def test_search_preserves_payload_and_scrubs_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payloads = [
        {'messages': [{'id': 'm1', 'threadId': 't1'}]},
        {
            'id': 'm1',
            'threadId': 't1',
            'payload': {
                'headers': [
                    {'name': 'Subject', 'value': 'private subject'},
                    {'name': 'From', 'value': 'private@example.com'},
                ]
            },
        },
    ]
    uris: list[str] = []
    closed: list[bool] = []

    def request(
        _: object, method: str, uri: str, **kwargs: Any
    ) -> requests.Response:
        """Return real SDK response."""
        uris.append(uri)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payloads.pop(0)).encode()
        return response

    monkeypatch.setattr(requests.Session, 'request', request)
    monkeypatch.setattr(
        requests.Session, 'close', lambda _: closed.append(True)
    )
    with caplog.at_level(logging.DEBUG):
        result = await run_gateway(
            GmailGateway(FakeCredentialStore()).search_messages,
            'from:private@example.com',
            1,
            None,
            read_budget=GmailReadBudget(),
        )
    assert result.items[0].subject == 'private subject'
    assert result.items[0].sender == 'private@example.com'
    assert len(uris) == 2
    assert 'private' in uris[0]
    assert closed == [True]
    assert 'phase=credentials' in caplog.text
    assert 'phase=service' in caplog.text
    assert 'phase=provider' in caplog.text
    assert 'private' not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_worker_cannot_start_later_provider_call() -> None:
    release = threading.Event()
    exited = threading.Event()
    provider_calls: list[str] = []

    def delayed() -> None:
        """Reject provider after cancellation."""
        from google_workspace_mcp.common.deadline import current_deadline

        try:
            release.wait(1)
            deadline = current_deadline()
            assert deadline is not None
            with deadline.measure('provider'):
                provider_calls.append('unexpected')
        finally:
            exited.set()

    try:
        with pytest.raises(GmailProviderError):
            await run_gateway(
                delayed,
                read_budget=GmailReadBudget(0.02, 0.01, 0.001),
            )
    finally:
        release.set()
    assert await asyncio.to_thread(exited.wait, 1)
    assert provider_calls == []


@pytest.mark.asyncio
async def test_timeout_reuses_existing_authenticated_request_id() -> None:
    release = threading.Event()
    principal = context.AuthenticatedPrincipal(
        principal_id='principal',
        credential_id='credential',
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request-identifier')
    try:
        with pytest.raises(
            GmailProviderError, match='request_id=request-identifier'
        ):
            await run_gateway(
                release.wait,
                1,
                read_budget=GmailReadBudget(0.02, 0.01, 0.001),
            )
    finally:
        release.set()
        context.reset_request_context(token)


@pytest.mark.parametrize(
    'key',
    [
        'GMAIL_READ_TOTAL_SECONDS',
        'GMAIL_READ_ATTEMPT_SECONDS',
        'GMAIL_READ_BACKOFF_SECONDS',
    ],
)
@pytest.mark.parametrize('value', ['', 'bad', '0', '-1', 'nan', 'inf'])
def test_environment_rejects_invalid_durations(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        GmailReadBudget.from_env({key: value})


@pytest.mark.parametrize(
    'service', ['gmail', 'calendar', 'drive', 'sheets', 'docs']
)
def test_read_budgets_are_scoped_to_gmail(
    service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = service.upper()
    monkeypatch.setenv(
        f'{prefix}_MCP_PUBLIC_URL', f'https://example.test/{service}'
    )
    monkeypatch.setenv(f'{prefix}_OAUTH_LOGIN_USERNAME', 'user')
    monkeypatch.setenv(f'{prefix}_OAUTH_LOGIN_PASSWORD', 'password')
    monkeypatch.setenv('GMAIL_READ_TOTAL_SECONDS', '12')
    monkeypatch.setenv('GMAIL_READ_ATTEMPT_SECONDS', '3')
    monkeypatch.setenv('GMAIL_READ_BACKOFF_SECONDS', '0.1')
    budget = ServiceConfig.from_env(service).gmail_read_budget
    if service == 'gmail':
        assert (
            budget.total_seconds,
            budget.attempt_seconds,
            budget.backoff_seconds,
        ) == (12, 3, 0.1)
    else:
        assert (
            budget.total_seconds,
            budget.attempt_seconds,
            budget.backoff_seconds,
        ) == (45, 10, 0.5)
        monkeypatch.setenv('GMAIL_READ_TOTAL_SECONDS', 'invalid')
        assert ServiceConfig.from_env(service).gmail_read_budget == budget


@pytest.mark.asyncio
async def test_sdk_preserves_long_unicode_search_without_logging_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        _: object, method: str, uri: str, **kwargs: Any
    ) -> requests.Response:
        """Record SDK query transmission."""
        calls.append((method, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"messages": []}'
        return response

    query = 'я' * 400
    monkeypatch.setattr(requests.Session, 'request', request)
    with caplog.at_level(logging.DEBUG):
        result = await run_gateway(
            GmailGateway(FakeCredentialStore()).search_messages,
            query,
            1,
            None,
            read_budget=GmailReadBudget(),
        )
    assert result.items == ()
    assert len(calls) == 1
    assert calls[0][0] == 'POST'
    assert calls[0][1]['headers']['x-http-method-override'] == 'GET'
    assert 'q=' in calls[0][1]['data']
    assert query not in caplog.text
    assert '%D1%8F' not in caplog.text


@pytest.mark.asyncio
async def test_extension_passes_configured_budget_to_mcp_tool(
    service_config: ServiceConfig,
) -> None:
    from dataclasses import replace

    from mcp.server.mcpserver.exceptions import ToolError

    from google_workspace_mcp.services.gmail.extension import GmailExtension
    from google_workspace_mcp.transport.authorization import (
        PolicyMCPServer,
        ToolRegistrar,
    )

    release = threading.Event()

    class DelayedGateway:
        """Delay synthetic label response."""

        def list_labels(self) -> None:
            """Wait for test release."""
            release.wait(1)

        def __getattr__(self, name: str) -> Any:
            """Supply unused registration operation."""
            return self.list_labels

    extension = GmailExtension(
        replace(
            service_config,
            gmail_read_budget=GmailReadBudget(0.02, 0.01, 0.001),
        ),
        gateway_factory=lambda _: DelayedGateway(),
    )
    server = PolicyMCPServer('gmail')
    extension.register_tools(ToolRegistrar(server))
    principal = context.AuthenticatedPrincipal(
        principal_id='principal',
        credential_id='credential',
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'mcp-request-id')
    try:
        async with asyncio.timeout(0.2):
            with pytest.raises(ToolError, match='phase=credentials') as error:
                await server.call_tool('gmail_list_labels', {})
        assert 'request_id=mcp-request-id' in str(error.value)
    finally:
        release.set()
        context.reset_request_context(token)
