"""Test confirmed fixture application."""

from __future__ import annotations

import base64
import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.discovery import build
from httplib2 import Response

from google_workspace_mcp.evals.apply import (
    FixtureApplicationError,
    apply_fixture,
    validate_seed_credentials,
)
from google_workspace_mcp.evals.catalog import EXPECTED_LOGICAL_REFS
from google_workspace_mcp.evals.cli import main
from google_workspace_mcp.evals.models import (
    ApplicationConfirmation,
    BindingState,
    FixtureBindings,
    ServiceName,
    load_bindings,
)
from google_workspace_mcp.evals.preview import (
    PartialFixtureStateError,
    build_preview,
    confirm_application,
)
from google_workspace_mcp.evals.requests import GoogleServiceSet

from .conftest import (
    EXPECTED_OPERATION_IDS,
    make_bindings,
    write_bindings,
)

# === Transport double ===

_PROVIDER_RESPONSES: tuple[dict[str, Any], ...] = (
    {
        'id': 'draft-cobalt',
        'message': {
            'id': 'message-draft-cobalt',
            'threadId': 'thread-draft-cobalt',
        },
    },
    {'id': 'message-alpha-root', 'threadId': 'thread-alpha'},
    {'id': 'message-alpha-reply', 'threadId': 'thread-alpha'},
    {'id': 'message-beta-root', 'threadId': 'thread-beta'},
    {'id': 'event-timed'},
    {'id': 'event-all-day'},
    {'id': 'event-recurring'},
    {'id': 'drive-folder'},
    {'id': 'drive-note'},
    {'id': 'drive-ledger'},
    {
        'spreadsheetId': 'spreadsheet-primary',
        'sheets': [
            {'properties': {'title': 'Inputs', 'sheetId': 41001}},
            {'properties': {'title': 'Summary', 'sheetId': 41002}},
        ],
    },
    {'totalUpdatedCells': 15},
    {
        'documentId': 'document-primary',
        'tabs': [
            {
                'tabProperties': {
                    'tabId': 'document-primary-tab',
                    'title': 'Tab 1',
                }
            }
        ],
    },
    {'writeControl': {'requiredRevisionId': 'revision-2'}},
)


def _validate_content_length(
    body: str | bytes | None,
    headers: Mapping[str, str],
) -> None:
    """Validate HTTP body framing."""
    payload = body.encode('utf-8') if isinstance(body, str) else body
    declared_length = headers.get('content-length')
    if payload is not None and declared_length is not None:
        assert int(declared_length) == len(payload), (
            f'content-length {declared_length} != body {len(payload)}'
        )


class RecordingGoogleHttp:
    """Return deterministic Google responses."""

    def __init__(self, fail_at: int | None = None) -> None:
        """Initialize test double."""
        self.fail_at = fail_at
        self.failed_once = False
        self.operation_index = 0
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        uri: str,
        method: str = 'GET',
        body: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        **_kwargs: Any,
    ) -> tuple[Response, bytes]:
        """Record one transport execution."""
        request_headers = dict(headers or {})
        _validate_content_length(body, request_headers)
        operation_id = EXPECTED_OPERATION_IDS[self.operation_index]
        body_text = body.decode('utf-8') if isinstance(body, bytes) else body
        parsed_body = json.loads(body_text) if body_text else {}
        self.calls.append(
            {
                'operation_id': operation_id,
                'uri': uri,
                'method': method,
                'body': parsed_body,
                'headers': request_headers,
            }
        )
        if self.fail_at == self.operation_index and not self.failed_once:
            self.failed_once = True
            response = Response(
                {'status': '503', 'content-type': 'application/json'}
            )
            return response, b'{"error":{"message":"unavailable"}}'
        payload = _PROVIDER_RESPONSES[self.operation_index]
        self.operation_index += 1
        response = Response(
            {'status': '200', 'content-type': 'application/json'}
        )
        return response, json.dumps(payload).encode('utf-8')

    def service_factory(self) -> GoogleServiceSet:
        """Build discovery clients."""
        common = {
            'http': self,
            'cache_discovery': False,
            'static_discovery': True,
        }
        return GoogleServiceSet(
            gmail=build('gmail', 'v1', **common),
            calendar=build('calendar', 'v3', **common),
            drive=build('drive', 'v3', **common),
            sheets=build('sheets', 'v4', **common),
            docs=build('docs', 'v1', **common),
        )


# === Registry fixtures ===


def _planned_bindings() -> FixtureBindings:
    """Build pristine private bindings."""
    return make_bindings(
        state=BindingState.PLANNED,
        logical_refs=frozenset(),
        applied_operations=frozenset(),
        owner_email='owner@private.test',
    )


def _write_planned_bindings(path: Path) -> FixtureBindings:
    """Persist pristine private bindings."""
    bindings = _planned_bindings()
    write_bindings(path, bindings)
    return bindings


def _confirmation(bindings: FixtureBindings) -> ApplicationConfirmation:
    """Confirm pristine fixture preview."""
    digest = build_preview(bindings).document.preview_digest
    return ApplicationConfirmation(
        fixture_version='stage12-v1',
        preview_digest=digest,
        acknowledge_writes=True,
    )


# === Application contract ===


@pytest.mark.parametrize('body', ['{}', b'{}'])
def test_transport_double_rejects_mismatched_content_length(
    body: str | bytes,
) -> None:
    transport = RecordingGoogleHttp()

    with pytest.raises(
        AssertionError,
        match='content-length 3 != body 2',
    ):
        transport.request(
            'https://example.invalid',
            method='POST',
            body=body,
            headers={'content-length': '3'},
        )

    assert transport.calls == []


def test_apply_executes_every_operation_and_persists_bindings(
    protected_json_file: Path,
) -> None:
    protected_json_file.parent.chmod(0o755)
    bindings = _write_planned_bindings(protected_json_file)
    transport = RecordingGoogleHttp()

    result = apply_fixture(
        protected_json_file,
        _confirmation(bindings),
        service_factory=transport.service_factory,
    )
    persisted = load_bindings(protected_json_file)

    assert result == persisted
    assert persisted.state is BindingState.APPLIED
    assert persisted.applied_operations == frozenset(EXPECTED_OPERATION_IDS)
    assert frozenset(persisted.objects) == EXPECTED_LOGICAL_REFS
    assert [call['operation_id'] for call in transport.calls] == list(
        EXPECTED_OPERATION_IDS
    )
    assert len(transport.calls) == 14
    assert stat.S_IMODE(protected_json_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(protected_json_file.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize('fail_at', range(14))
def test_failure_persists_exact_successful_prefix_without_retry(
    protected_json_file: Path,
    fail_at: int,
) -> None:
    bindings = _write_planned_bindings(protected_json_file)
    initial = build_preview(bindings)
    transport = RecordingGoogleHttp(fail_at=fail_at)

    with pytest.raises(
        FixtureApplicationError,
        match=EXPECTED_OPERATION_IDS[fail_at],
    ) as captured_error:
        apply_fixture(
            protected_json_file,
            _confirmation(bindings),
            service_factory=transport.service_factory,
        )

    persisted = load_bindings(protected_json_file)
    expected_prefix = EXPECTED_OPERATION_IDS[:fail_at]
    expected_refs = frozenset(
        logical_ref
        for operation in initial.document.operations[:fail_at]
        for logical_ref in operation.logical_outputs
    )
    remaining = build_preview(persisted)
    remaining_ids = tuple(
        operation.operation_id for operation in remaining.document.operations
    )

    assert persisted.state is BindingState.PLANNED
    assert persisted.applied_operations == frozenset(expected_prefix)
    assert frozenset(persisted.objects) == expected_refs
    assert remaining_ids == EXPECTED_OPERATION_IDS[fail_at:]
    assert len(transport.calls) == fail_at + 1
    assert captured_error.value.__cause__ is None
    assert 'owner@private.test' not in str(captured_error.value)
    assert 'primary-calendar-private-value' not in str(captured_error.value)
    if expected_prefix:
        assert remaining.document.blocked_reason == 'partial_state'
        with pytest.raises(PartialFixtureStateError):
            confirm_application(
                remaining,
                ApplicationConfirmation(
                    fixture_version='stage12-v1',
                    preview_digest=remaining.document.preview_digest,
                    acknowledge_writes=True,
                ),
            )
    else:
        assert remaining.document.application_allowed is True


def test_partial_registry_refuses_repeated_application(
    protected_json_file: Path,
) -> None:
    bindings = _write_planned_bindings(protected_json_file)
    failing_transport = RecordingGoogleHttp(fail_at=3)
    with pytest.raises(FixtureApplicationError):
        apply_fixture(
            protected_json_file,
            _confirmation(bindings),
            service_factory=failing_transport.service_factory,
        )
    persisted = load_bindings(protected_json_file)
    remaining = build_preview(persisted)
    replacement_transport = RecordingGoogleHttp()

    with pytest.raises(PartialFixtureStateError):
        apply_fixture(
            protected_json_file,
            ApplicationConfirmation(
                fixture_version='stage12-v1',
                preview_digest=remaining.document.preview_digest,
                acknowledge_writes=True,
            ),
            service_factory=replacement_transport.service_factory,
        )

    assert replacement_transport.calls == []


def test_digest_mismatch_executes_nothing_and_preserves_registry(
    protected_json_file: Path,
) -> None:
    _write_planned_bindings(protected_json_file)
    original = protected_json_file.read_bytes()
    transport = RecordingGoogleHttp()
    confirmation = ApplicationConfirmation(
        fixture_version='stage12-v1',
        preview_digest='0' * 64,
        acknowledge_writes=True,
    )

    with pytest.raises(ValueError, match='digest does not match'):
        apply_fixture(
            protected_json_file,
            confirmation,
            service_factory=transport.service_factory,
        )

    assert transport.calls == []
    assert protected_json_file.read_bytes() == original


@pytest.mark.parametrize(
    'missing_value', ['owner_email', 'calendar_primary_id']
)
def test_missing_private_input_executes_nothing(
    protected_json_file: Path,
    missing_value: str,
) -> None:
    bindings = _planned_bindings().model_copy(update={missing_value: None})
    write_bindings(protected_json_file, bindings)
    transport = RecordingGoogleHttp()

    with pytest.raises(ValueError, match=f'{missing_value} is required'):
        apply_fixture(
            protected_json_file,
            _confirmation(bindings),
            service_factory=transport.service_factory,
        )

    assert transport.calls == []
    persisted = load_bindings(protected_json_file)
    assert persisted.applied_operations == frozenset()


def test_version_mismatch_executes_nothing_and_preserves_registry(
    protected_json_file: Path,
) -> None:
    bindings = _write_planned_bindings(protected_json_file)
    original = protected_json_file.read_bytes()
    transport = RecordingGoogleHttp()
    confirmation = ApplicationConfirmation.model_construct(
        fixture_version='stage11-v1',
        preview_digest=build_preview(bindings).document.preview_digest,
        acknowledge_writes=True,
    )

    with pytest.raises(ValueError, match='version does not match'):
        apply_fixture(
            protected_json_file,
            confirmation,
            service_factory=transport.service_factory,
        )

    assert transport.calls == []
    assert protected_json_file.read_bytes() == original


def test_atomic_save_failure_keeps_registry_readable(
    protected_json_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = _write_planned_bindings(protected_json_file)
    transport = RecordingGoogleHttp()

    def fail_replace(*_args: Any, **_kwargs: Any) -> None:
        """Interrupt the atomic replacement."""
        raise OSError('simulated interruption')

    monkeypatch.setattr(
        'google_workspace_mcp.evals.apply.os.replace',
        fail_replace,
    )

    with pytest.raises(FixtureApplicationError):
        apply_fixture(
            protected_json_file,
            _confirmation(bindings),
            service_factory=transport.service_factory,
        )

    persisted = load_bindings(protected_json_file)
    assert persisted.state is BindingState.PLANNED
    assert persisted.applied_operations == frozenset()
    assert persisted.objects == {}


def test_executed_requests_resolve_private_and_provider_values(
    protected_json_file: Path,
) -> None:
    bindings = _write_planned_bindings(protected_json_file)
    transport = RecordingGoogleHttp()

    apply_fixture(
        protected_json_file,
        _confirmation(bindings),
        service_factory=transport.service_factory,
    )

    serialized = json.dumps(transport.calls)
    gmail_calls = transport.calls[:4]
    decoded_messages = [
        base64.urlsafe_b64decode(call['body']['raw']).decode('utf-8')
        if 'raw' in call['body']
        else base64.urlsafe_b64decode(call['body']['message']['raw']).decode(
            'utf-8'
        )
        for call in gmail_calls
    ]

    assert '__binding_' not in serialized
    assert all('To: owner@private.test' in item for item in decoded_messages)
    assert transport.calls[2]['body']['threadId'] == 'thread-alpha'
    assert all(
        'primary-calendar-private-value' in call['uri']
        for call in transport.calls[4:7]
    )
    assert transport.calls[8]['body']['parents'] == ['drive-folder']
    assert transport.calls[9]['body']['parents'] == ['drive-folder']
    assert 'spreadsheet-primary' in transport.calls[11]['uri']
    assert 'document-primary' in transport.calls[13]['uri']
    location = transport.calls[13]['body']['requests'][0]['insertText'][
        'location'
    ]
    assert location['tabId'] == 'document-primary-tab'


def test_public_preview_excludes_private_application_values() -> None:
    bindings = _planned_bindings()

    output = build_preview(bindings).document.model_dump_json()

    assert 'owner@private.test' not in output
    assert 'primary-calendar-private-value' not in output
    assert 'bindings.owner_email' in output
    assert 'bindings.calendar_primary_id' in output


# === Credential path contract ===


def _credential_directory(tmp_path: Path) -> Path:
    """Create protected credential paths."""
    credentials_dir = tmp_path / 'google-tokens'
    credentials_dir.mkdir(mode=0o700)
    for service in ServiceName:
        credential = credentials_dir / f'{service.value}.json'
        credential.write_text('{}', encoding='utf-8')
        credential.chmod(0o600)
    return credentials_dir


def test_seed_credentials_require_protected_directory_and_files(
    tmp_path: Path,
) -> None:
    credentials_dir = _credential_directory(tmp_path)

    paths = validate_seed_credentials(credentials_dir)

    assert paths == {
        service: credentials_dir / f'{service.value}.json'
        for service in ServiceName
    }


@pytest.mark.parametrize('unsafe_target', ['directory', 'file', 'symlink'])
def test_seed_credentials_reject_unsafe_paths(
    tmp_path: Path,
    unsafe_target: str,
) -> None:
    credentials_dir = _credential_directory(tmp_path)
    if unsafe_target == 'directory':
        credentials_dir.chmod(0o755)
    elif unsafe_target == 'file':
        (credentials_dir / 'gmail.json').chmod(0o640)
    else:
        target = credentials_dir / 'gmail.json'
        replacement = credentials_dir / 'gmail-real.json'
        target.rename(replacement)
        target.symlink_to(replacement)

    with pytest.raises(ValueError, match='seed credential'):
        validate_seed_credentials(credentials_dir)


# === CLI contract ===


def test_apply_cli_requires_explicit_credentials_directory(
    protected_json_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operation_calls: list[Path] = []

    def fake_apply_fixture(
        _bindings_path: Path,
        _confirmation: ApplicationConfirmation,
        *,
        credentials_dir: Path,
    ) -> FixtureBindings:
        """Record unexpected fixture execution."""
        operation_calls.append(credentials_dir)
        return make_bindings()

    monkeypatch.setattr(
        'google_workspace_mcp.evals.cli.apply_fixture',
        fake_apply_fixture,
    )

    with pytest.raises(SystemExit, match='2'):
        main(
            [
                'apply',
                '--bindings',
                str(protected_json_file),
                '--fixture-version',
                'stage12-v1',
                '--preview-digest',
                'a' * 64,
                '--acknowledge-writes',
            ]
        )

    assert '--credentials-dir' in capsys.readouterr().err
    assert operation_calls == []


def test_apply_cli_passes_explicit_confirmation(
    protected_json_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    credentials_dir = tmp_path / 'google-tokens'

    def fake_apply_fixture(
        bindings_path: Path,
        confirmation: ApplicationConfirmation,
        *,
        credentials_dir: Path,
    ) -> FixtureBindings:
        """Capture parsed CLI arguments."""
        captured['bindings_path'] = bindings_path
        captured['confirmation'] = confirmation
        captured['credentials_dir'] = credentials_dir
        return make_bindings()

    monkeypatch.setattr(
        'google_workspace_mcp.evals.cli.apply_fixture',
        fake_apply_fixture,
    )

    exit_code = main(
        [
            'apply',
            '--bindings',
            str(protected_json_file),
            '--credentials-dir',
            str(credentials_dir),
            '--fixture-version',
            'stage12-v1',
            '--preview-digest',
            'a' * 64,
            '--acknowledge-writes',
        ]
    )

    output = json.loads(capsys.readouterr().out)
    confirmation = captured['confirmation']
    assert exit_code == 0
    assert captured['bindings_path'] == protected_json_file
    assert captured['credentials_dir'] == credentials_dir
    assert confirmation.preview_digest == 'a' * 64
    assert confirmation.acknowledge_writes is True
    assert output == {
        'fixture_version': 'stage12-v1',
        'state': 'applied',
        'applied_operation_count': 14,
    }
