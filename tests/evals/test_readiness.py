"""Test fixture readiness checks."""

from __future__ import annotations

import json
import stat
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import google_workspace_mcp.evals.cli as eval_cli
from google_workspace_mcp.evals.catalog import (
    EXPECTED_LOGICAL_REFS,
    MARKER_MESSAGE_ALPHA_REPLY,
    MARKER_MESSAGE_ALPHA_ROOT,
    MARKER_MESSAGE_BETA_ROOT,
)
from google_workspace_mcp.evals.models import (
    BindingState,
    FixtureBindings,
    ObjectBinding,
    load_bindings,
)
from google_workspace_mcp.evals.readiness import (
    FixtureReadinessError,
    GoogleReadinessProbe,
    check_readiness,
    mark_bindings_ready,
    require_ready_for_xml,
)
from google_workspace_mcp.evals.requests import GoogleServiceSet

from .conftest import make_bindings, write_bindings


class RecordingReadinessProbe:
    """Record bounded readiness probes."""

    def __init__(self, unavailable_marker: str | None = None) -> None:
        """Configure one missing delivery."""
        self.unavailable_marker = unavailable_marker
        self.delivery_calls: list[tuple[str, int]] = []
        self.object_calls: list[str] = []

    def find_gmail_delivery(
        self,
        *,
        exact_marker: str,
        max_results: int,
    ) -> bool:
        """Record exact Gmail search."""
        self.delivery_calls.append((exact_marker, max_results))
        return exact_marker != self.unavailable_marker

    def object_exists(self, binding: ObjectBinding) -> bool:
        """Record direct object read."""
        self.object_calls.append(binding.logical_ref)
        return True


class FakeRequest:
    """Return one configured response."""

    def __init__(self, value: dict[str, Any]) -> None:
        """Initialize fake request."""
        self.value = value
        self.retries: list[int] = []

    def execute(self, *, num_retries: int) -> dict[str, Any]:
        """Record readonly execution."""
        self.retries.append(num_retries)
        return self.value


class FakeMessages:
    """Record Gmail list arguments."""

    def __init__(self) -> None:
        """Initialize Gmail messages fake."""
        self.kwargs: dict[str, Any] | None = None
        self.request = FakeRequest(
            {'messages': [{'id': 'synthetic', 'threadId': 'thread'}]}
        )

    def list(self, **kwargs: Any) -> FakeRequest:
        """Record one Gmail search."""
        self.kwargs = kwargs
        return self.request


class FakeUsers:
    """Expose Gmail messages fake."""

    def __init__(self, messages: FakeMessages) -> None:
        """Initialize Gmail users fake."""
        self._messages = messages

    def messages(self) -> FakeMessages:
        """Return Gmail messages fake."""
        return self._messages


class FakeGmail:
    """Expose Gmail users fake."""

    def __init__(self, messages: FakeMessages) -> None:
        """Initialize Gmail service fake."""
        self._users = FakeUsers(messages)

    def users(self) -> FakeUsers:
        """Return Gmail users fake."""
        return self._users


# === CLI doubles ===


def _readiness_arguments(
    bindings_path: Path,
    credentials_dir: Path,
) -> list[str]:
    """Build readiness CLI arguments."""
    return [
        'readiness',
        '--bindings',
        str(bindings_path),
        '--credentials-dir',
        str(credentials_dir),
    ]


def _install_readiness_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    probe: RecordingReadinessProbe,
) -> tuple[dict[str, list[Any]], GoogleServiceSet]:
    """Install readiness CLI doubles."""
    services = GoogleServiceSet(
        gmail=None,
        calendar=None,
        drive=None,
        sheets=None,
        docs=None,
    )
    captured: dict[str, list[Any]] = {
        'credentials_dirs': [],
        'services': [],
        'calendar_ids': [],
    }

    def fake_build_services(credentials_dir: Path) -> GoogleServiceSet:
        """Build fake Google services."""
        captured['credentials_dirs'].append(credentials_dir)
        return services

    def fake_probe(
        service_set: GoogleServiceSet,
        *,
        calendar_primary_id: str,
    ) -> RecordingReadinessProbe:
        """Return configured readiness probe."""
        captured['services'].append(service_set)
        captured['calendar_ids'].append(calendar_primary_id)
        return probe

    monkeypatch.setattr(
        eval_cli,
        'build_application_services',
        fake_build_services,
    )
    monkeypatch.setattr(eval_cli, 'GoogleReadinessProbe', fake_probe)
    return captured, services


def test_readiness_checks_every_binding_once(
    applied_bindings: FixtureBindings,
) -> None:
    probe = RecordingReadinessProbe()

    report = check_readiness(applied_bindings, probe)

    assert report.status == 'ready'
    assert report.probe_count == len(EXPECTED_LOGICAL_REFS)
    assert Counter(probe.delivery_calls) == {
        (MARKER_MESSAGE_ALPHA_ROOT, 1): 1,
        (MARKER_MESSAGE_ALPHA_REPLY, 1): 1,
        (MARKER_MESSAGE_BETA_ROOT, 1): 1,
    }
    assert len(probe.object_calls) == len(EXPECTED_LOGICAL_REFS) - 3


def test_missing_gmail_delivery_returns_not_ready_without_retry(
    applied_bindings: FixtureBindings,
) -> None:
    probe = RecordingReadinessProbe(MARKER_MESSAGE_ALPHA_REPLY)

    report = check_readiness(applied_bindings, probe)

    assert report.status == 'not_ready'
    assert Counter(probe.delivery_calls)[(MARKER_MESSAGE_ALPHA_REPLY, 1)] == 1
    missing = next(
        item
        for item in report.items
        if item.logical_ref == 'gmail_delivery_alpha_reply'
    )
    assert missing.status == 'not_ready'
    with pytest.raises(ValueError, match='fixture is not ready'):
        mark_bindings_ready(applied_bindings, report)


def test_google_readiness_uses_one_exact_gmail_search() -> None:
    messages = FakeMessages()
    services = GoogleServiceSet(
        gmail=FakeGmail(messages),
        calendar=None,
        drive=None,
        sheets=None,
        docs=None,
    )
    probe = GoogleReadinessProbe(
        services,
        calendar_primary_id='private-calendar-id',
    )

    found = probe.find_gmail_delivery(
        exact_marker=MARKER_MESSAGE_ALPHA_ROOT,
        max_results=1,
    )

    assert found is True
    assert messages.kwargs == {
        'userId': 'me',
        'q': f'"{MARKER_MESSAGE_ALPHA_ROOT}"',
        'maxResults': 1,
        'fields': 'messages(id,threadId)',
    }
    assert messages.request.retries == [0]


def test_readiness_refuses_planned_bindings() -> None:
    bindings = make_bindings(
        state=BindingState.PLANNED,
        logical_refs=frozenset(),
        applied_operations=frozenset(),
    )

    with pytest.raises(ValueError, match='planned bindings'):
        check_readiness(bindings, RecordingReadinessProbe())


def test_missing_binding_is_not_probed() -> None:
    bindings = make_bindings(
        logical_refs=frozenset({'drive_fixture_folder'}),
        applied_operations=frozenset({'drive.create.folder'}),
    )
    probe = RecordingReadinessProbe()

    report = check_readiness(bindings, probe)

    assert report.status == 'not_ready'
    assert report.probe_count == 1
    assert probe.object_calls == ['drive_fixture_folder']
    assert probe.delivery_calls == []


def test_xml_authoring_requires_ready_state(
    applied_bindings: FixtureBindings,
) -> None:
    with pytest.raises(ValueError, match='requires ready'):
        require_ready_for_xml(applied_bindings)

    probe = RecordingReadinessProbe()
    report = check_readiness(applied_bindings, probe)
    ready = mark_bindings_ready(applied_bindings, report)

    require_ready_for_xml(ready)


# === Readiness CLI ===


def test_readiness_cli_requires_explicit_credentials_directory(
    protected_json_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match='2'):
        eval_cli.main(
            [
                'readiness',
                '--bindings',
                str(protected_json_file),
            ]
        )

    assert '--credentials-dir' in capsys.readouterr().err


def test_readiness_cli_promotes_complete_registry(
    protected_json_file: Path,
    applied_bindings: FixtureBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_bindings(protected_json_file, applied_bindings)
    probe = RecordingReadinessProbe()
    captured, services = _install_readiness_dependencies(monkeypatch, probe)
    credentials_dir = tmp_path / 'google-tokens'

    exit_code = eval_cli.main(
        _readiness_arguments(protected_json_file, credentials_dir)
    )

    persisted = load_bindings(protected_json_file)
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert persisted.state is BindingState.READY
    assert stat.S_IMODE(protected_json_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(protected_json_file.parent.stat().st_mode) == 0o700
    assert captured == {
        'credentials_dirs': [credentials_dir],
        'services': [services],
        'calendar_ids': ['primary-calendar-private-value'],
    }
    assert output == {
        'binding_state': 'ready',
        'fixture_version': 'stage12-v1',
        'not_ready_count': 0,
        'probe_count': 21,
        'readiness_status': 'ready',
        'ready_count': 21,
    }


def test_readiness_cli_preserves_not_ready_registry(
    protected_json_file: Path,
    applied_bindings: FixtureBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_bindings(protected_json_file, applied_bindings)
    original = protected_json_file.read_bytes()
    probe = RecordingReadinessProbe(MARKER_MESSAGE_ALPHA_REPLY)
    _install_readiness_dependencies(monkeypatch, probe)

    exit_code = eval_cli.main(
        _readiness_arguments(
            protected_json_file,
            tmp_path / 'google-tokens',
        )
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert protected_json_file.read_bytes() == original
    assert output == {
        'binding_state': 'applied',
        'fixture_version': 'stage12-v1',
        'not_ready_count': 1,
        'probe_count': 21,
        'readiness_status': 'not_ready',
        'ready_count': 20,
    }


def test_readiness_cli_rejects_planned_before_credentials(
    protected_json_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = make_bindings(
        state=BindingState.PLANNED,
        logical_refs=frozenset(),
        applied_operations=frozenset(),
    )
    write_bindings(protected_json_file, bindings)
    credential_calls: list[Path] = []

    def fake_build_services(credentials_dir: Path) -> GoogleServiceSet:
        """Record unexpected credential access."""
        credential_calls.append(credentials_dir)
        return GoogleServiceSet(None, None, None, None, None)

    monkeypatch.setattr(
        eval_cli,
        'build_application_services',
        fake_build_services,
    )

    with pytest.raises(ValueError, match='planned bindings'):
        eval_cli.main(
            _readiness_arguments(
                protected_json_file,
                tmp_path / 'google-tokens',
            )
        )

    assert credential_calls == []


@pytest.mark.parametrize('calendar_primary_id', [None, ''])
def test_readiness_cli_requires_calendar_before_credentials(
    protected_json_file: Path,
    applied_bindings: FixtureBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calendar_primary_id: str | None,
) -> None:
    secret_value = (
        SecretStr(calendar_primary_id)
        if calendar_primary_id is not None
        else None
    )
    bindings = applied_bindings.model_copy(
        update={'calendar_primary_id': secret_value}
    )
    write_bindings(protected_json_file, bindings)
    credential_calls: list[Path] = []

    def fake_build_services(credentials_dir: Path) -> GoogleServiceSet:
        """Record unexpected credential access."""
        credential_calls.append(credentials_dir)
        return GoogleServiceSet(None, None, None, None, None)

    monkeypatch.setattr(
        eval_cli,
        'build_application_services',
        fake_build_services,
    )

    with pytest.raises(
        ValueError,
        match='calendar_primary_id is required',
    ):
        eval_cli.main(
            _readiness_arguments(
                protected_json_file,
                tmp_path / 'google-tokens',
            )
        )

    assert credential_calls == []


def test_readiness_cli_sanitizes_provider_failure(
    protected_json_file: Path,
    applied_bindings: FixtureBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_bindings(protected_json_file, applied_bindings)
    original = protected_json_file.read_bytes()
    _install_readiness_dependencies(
        monkeypatch,
        RecordingReadinessProbe(),
    )

    def fail_readiness(
        _bindings: FixtureBindings,
        _probe: RecordingReadinessProbe,
    ) -> None:
        """Raise private provider failure."""
        raise RuntimeError('owner@private.test')

    monkeypatch.setattr(
        eval_cli,
        'check_readiness',
        fail_readiness,
    )

    with pytest.raises(
        FixtureReadinessError,
        match='fixture readiness check failed',
    ) as captured_error:
        eval_cli.main(
            _readiness_arguments(
                protected_json_file,
                tmp_path / 'google-tokens',
            )
        )

    assert captured_error.value.__cause__ is None
    assert 'owner@private.test' not in str(captured_error.value)
    assert protected_json_file.read_bytes() == original
