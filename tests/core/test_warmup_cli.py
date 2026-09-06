"""Test Google credential warm-up entrypoint."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from google_workspace_mcp.cli import SERVICES, warmup
from google_workspace_mcp.google_auth.credentials import GoogleCredentials
from google_workspace_mcp.google_auth.errors import (
    GoogleAuthError,
    TokenRevokedError,
)

SECRET_TOKEN = 'access-token-secret-value'  # noqa: S105
SECRET_REFRESH = 'refresh-token-secret-value'  # noqa: S105
SECRET_CLIENT = 'client-secret-secret-value'  # noqa: S105


def _credentials() -> GoogleCredentials:
    """Build synthetic warm credentials."""
    return GoogleCredentials(
        token=SECRET_TOKEN,
        refresh_token=SECRET_REFRESH,
        token_uri='https://oauth2.googleapis.com/token',
        client_id='client-id',
        client_secret=SECRET_CLIENT,
        scopes=('https://www.googleapis.com/auth/drive.file',),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )


class RecordingStore:
    """Record credential refresh calls."""

    def __init__(
        self,
        service: str,
        *,
        error: Exception | None = None,
    ) -> None:
        """Initialize recording credential store."""
        self.service = service
        self.path = Path(f'/nonexistent/{service}/google_token.json')
        self.refresh_calls: list[bool] = []
        self._error = error

    def refresh(self, request: Any = None, *, force: bool = False) -> Any:
        """Record one refresh call."""
        self.refresh_calls.append(force)
        if self._error is not None:
            raise self._error
        return _credentials()


def _harness(
    *,
    failing: dict[str, Exception] | None = None,
) -> tuple[Any, dict[str, RecordingStore], list[tuple[str, Any]]]:
    """Build warm-up test doubles."""
    failures = failing or {}
    stores = {
        service: RecordingStore(service, error=failures.get(service))
        for service in SERVICES
    }
    probes: list[tuple[str, Any]] = []

    def store_factory(service: str) -> Any:
        return stores[service]

    def probe_runner(service: str, credentials: Any) -> None:
        probes.append((service, credentials))

    return (store_factory, probe_runner), stores, probes


def _run(
    argv: list[str],
    *,
    failing: dict[str, Exception] | None = None,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], Any]:
    """Run warm-up entrypoint against doubles."""
    (store_factory, probe_runner), stores, probes = _harness(failing=failing)
    out = io.StringIO()
    errors = io.StringIO()
    code = warmup.main(
        argv,
        out=out,
        errors=errors,
        store_factory=store_factory,
        probe_runner=probe_runner,
    )
    parsed_out = [
        json.loads(line) for line in out.getvalue().splitlines() if line
    ]
    parsed_errors = [
        json.loads(line) for line in errors.getvalue().splitlines() if line
    ]
    return code, parsed_out, parsed_errors, (stores, probes, out, errors)


def test_warms_every_service_by_default() -> None:
    code, reports, errors, (stores, probes, _, _) = _run([])

    assert code == 0
    assert errors == []
    # Per-service assertion: a bare count would pass if one service
    # were warmed five times and the others never touched.
    assert [service for service, _ in probes] == list(SERVICES)
    for service in SERVICES:
        assert stores[service].refresh_calls == [True]
    assert [report['service'] for report in reports] == list(SERVICES)


def test_forces_the_token_exchange() -> None:
    _, _, _, (stores, _, _, _) = _run(['--service', 'gmail'])

    assert stores['gmail'].refresh_calls == [True]


def test_single_service_leaves_others_untouched() -> None:
    code, reports, _, (stores, probes, _, _) = _run(['--service', 'sheets'])

    assert code == 0
    assert [service for service, _ in probes] == ['sheets']
    assert [report['service'] for report in reports] == ['sheets']
    for service in SERVICES:
        expected = [True] if service == 'sheets' else []
        assert stores[service].refresh_calls == expected


def test_provider_failure_exits_non_zero_and_keeps_going() -> None:
    failure = TokenRevokedError('Google authorization requires renewal')
    code, reports, errors, (stores, probes, _, _) = _run(
        [],
        failing={'drive': failure},
    )

    assert code == 1
    assert [error['service'] for error in errors] == ['drive']
    assert errors[0]['error'] == 'Google authorization requires renewal'
    # The remaining four services are still warmed.
    assert [service for service, _ in probes] == [
        service for service in SERVICES if service != 'drive'
    ]
    assert [report['service'] for report in reports] == [
        service for service in SERVICES if service != 'drive'
    ]


def test_unexpected_failure_reports_without_internals() -> None:
    code, _, errors, _ = _run(
        ['--service', 'docs'],
        failing={'docs': RuntimeError(SECRET_REFRESH)},
    )

    assert code == 1
    assert SECRET_REFRESH not in errors[0]['error']
    assert errors[0]['error'] == (
        'warm-up failed before the credential was refreshed'
    )


def test_output_never_carries_secret_values() -> None:
    _, _, _, (_, _, out, errors) = _run(
        [],
        failing={'gmail': GoogleAuthError(SECRET_TOKEN)},
    )
    combined = out.getvalue() + errors.getvalue()

    assert combined  # a silent run would pass every absence assertion
    for secret in (SECRET_TOKEN, SECRET_REFRESH, SECRET_CLIENT):
        assert secret not in combined


def test_report_names_what_was_done() -> None:
    _, reports, _, _ = _run(['--service', 'calendar'])

    report = reports[0]
    assert report['service'] == 'calendar'
    assert report['refreshed'] is True
    assert report['probe'] == 'calendar.calendarList.list'
    assert report['token_path'].endswith('google_token.json')
    assert report['expiry']


def test_rejects_unknown_service() -> None:
    with pytest.raises(SystemExit):
        warmup.main(['--service', 'contacts'])


def test_default_store_binds_service_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv('DOCS_GOOGLE_TOKEN_PATH', str(tmp_path / 'token.json'))
    monkeypatch.setenv('DOCS_MCP_DOWNLOAD_PATH', str(tmp_path / 'downloads'))

    store = warmup._default_store('docs')

    assert store.path == tmp_path / 'token.json'
    assert store.required_scopes == warmup.SERVICE_SCOPES['docs']


class RecordingRequest:
    """Record one discovery request."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        """Initialize recording request."""
        self._calls = calls

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Record request execution."""
        self._calls.append(('execute', kwargs))
        return {}


class RecordingResource:
    """Record discovery resource access."""

    def __init__(
        self,
        name: str,
        calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Initialize recording resource."""
        self._name = name
        self._calls = calls

    def __getattr__(self, item: str) -> Any:
        """Return recording child resource."""

        def _call(**kwargs: Any) -> Any:
            path = f'{self._name}.{item}'
            if kwargs:
                self._calls.append((path, kwargs))
                return RecordingRequest(self._calls)
            self._calls.append((path, {}))
            return RecordingResource(path, self._calls)

        return _call


_BUILDERS = {
    'gmail': 'build_gmail_service',
    'calendar': 'build_calendar_service',
    'drive': 'build_drive_service',
}

_RETRIES = {'num_retries': warmup.WARMUP_REQUEST_RETRIES}

_EXPECTED_CHAINS: dict[str, tuple[str, list[tuple[str, dict[str, Any]]]]] = {
    'gmail': (
        'gmail',
        [
            ('gmail.users', {}),
            ('gmail.users.getProfile', {'userId': 'me'}),
            ('execute', _RETRIES),
        ],
    ),
    'calendar': (
        'calendar',
        [
            ('calendar.calendarList', {}),
            (
                'calendar.calendarList.list',
                {'maxResults': 1, 'fields': 'items(id)'},
            ),
            ('execute', _RETRIES),
        ],
    ),
    'drive': (
        'drive',
        [
            ('drive.files', {}),
            ('drive.files.list', {'pageSize': 1, 'fields': 'files(id)'}),
            ('execute', _RETRIES),
        ],
    ),
    'sheets': (
        'drive',
        [
            ('drive.files', {}),
            ('drive.files.list', {'pageSize': 1, 'fields': 'files(id)'}),
            ('execute', _RETRIES),
        ],
    ),
    'docs': (
        'drive',
        [
            ('drive.files', {}),
            ('drive.files.list', {'pageSize': 1, 'fields': 'files(id)'}),
            ('execute', _RETRIES),
        ],
    ),
}


@pytest.mark.parametrize('service', SERVICES)
def test_default_probe_calls_one_read_only_method(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    used_api, expected_chain = _EXPECTED_CHAINS[service]
    # Patch every builder, each recording into its own list, so reaching
    # the wrong API is visible instead of being handed the right label.
    recorded: dict[str, list[tuple[str, dict[str, Any]]]] = {
        api: [] for api in _BUILDERS
    }

    for api, builder_name in _BUILDERS.items():

        def fake_builder(
            credentials: GoogleCredentials,
            _api: str = api,
        ) -> Any:
            return RecordingResource(_api, recorded[_api])

        monkeypatch.setattr(warmup, builder_name, fake_builder)

    warmup._default_probe(service, _credentials())

    # Whole chain, unfiltered: an extra call (a write, a delete) breaks
    # equality instead of being discarded by a name filter.
    assert recorded[used_api] == expected_chain
    for api, calls in recorded.items():
        if api != used_api:
            assert calls == [], f'{service} unexpectedly reached {api}'


class RecordingProbeStore:
    """Refresh successfully, then let the probe fail."""

    def __init__(self, service: str) -> None:
        """Initialize probe-stage store."""
        self.service = service
        self.path = Path(f'/nonexistent/{service}/google_token.json')
        self.refresh_calls: list[bool] = []

    def refresh(self, request: Any = None, *, force: bool = False) -> Any:
        """Record a successful refresh."""
        self.refresh_calls.append(force)
        return _credentials()


def _run_probe_failure(exc: Exception) -> tuple[int, list[dict[str, Any]]]:
    """Run warm-up whose probe raises."""
    store = RecordingProbeStore('drive')
    errors = io.StringIO()

    def probe_runner(service: str, credentials: Any) -> None:
        raise exc

    code = warmup.main(
        ['--service', 'drive'],
        out=io.StringIO(),
        errors=errors,
        store_factory=lambda service: store,
        probe_runner=probe_runner,
    )
    parsed = [
        json.loads(line) for line in errors.getvalue().splitlines() if line
    ]
    return code, parsed


def test_probe_failure_states_the_credential_was_refreshed() -> None:
    from googleapiclient.errors import HttpError

    response = type('R', (), {'status': 403, 'reason': 'Forbidden'})()
    code, errors = _run_probe_failure(HttpError(resp=response, content=b'{}'))

    assert code == 1
    # The exchange already happened; saying otherwise sends the operator to
    # re-run the consent flow and burn a refresh token.
    assert errors[0]['refreshed'] is True
    assert 'before the credential was refreshed' not in errors[0]['error']
    assert '403' in errors[0]['error']


def test_probe_transport_failure_is_not_a_path_problem() -> None:
    import socket

    code, errors = _run_probe_failure(
        socket.gaierror(-2, 'Name or service not known')
    )

    assert code == 1
    assert errors[0]['refreshed'] is True
    assert 'credential path' not in errors[0]['error']
    assert 'reach' in errors[0]['error']


def test_refresh_failure_is_marked_as_not_refreshed() -> None:
    code, _, errors, _ = _run(
        ['--service', 'gmail'],
        failing={'gmail': TokenRevokedError('x')},
    )

    assert code == 1
    assert errors[0]['refreshed'] is False
    assert errors[0]['error'] == 'Google authorization requires renewal'
