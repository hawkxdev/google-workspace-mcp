"""Test fixture readiness checks."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

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
)
from google_workspace_mcp.evals.readiness import (
    GoogleReadinessProbe,
    check_readiness,
    mark_bindings_ready,
    require_ready_for_xml,
)
from google_workspace_mcp.evals.requests import GoogleServiceSet

from .conftest import make_bindings


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
