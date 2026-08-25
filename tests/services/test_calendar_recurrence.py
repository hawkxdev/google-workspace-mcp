"""Test Calendar recurrence semantics."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.services.calendar.errors import CalendarInputError
from google_workspace_mcp.services.calendar.recurrence import (
    RecurringEventMutator,
    split_recurrence,
)
from google_workspace_mcp.services.calendar.schemas import (
    EventMutationResult,
    SendUpdates,
)


def test_split_recurrence_truncates_before_timed_occurrence() -> None:
    original, following = split_recurrence(
        ('RRULE:FREQ=WEEKLY;BYDAY=TU',),
        '2026-09-01T10:00:00+00:00',
    )
    assert original == ('RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20260901T095959Z',)
    assert following == ('RRULE:FREQ=WEEKLY;BYDAY=TU',)


def test_split_recurrence_rejects_counted_rule() -> None:
    with pytest.raises(CalendarInputError, match='COUNT'):
        split_recurrence(
            ('RRULE:FREQ=DAILY;COUNT=10',),
            '2026-09-01T10:00:00Z',
        )


class FakeGateway:
    """Record recurring mutation operations."""

    def __init__(self, fail_create: bool = False) -> None:
        """Initialize recurrence gateway fake."""
        self.fail_create = fail_create
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.resource = {
            'id': 'series-1',
            'etag': 'etag-1',
            'summary': 'Standup',
            'start': {
                'dateTime': '2026-08-25T10:00:00+00:00',
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': '2026-08-25T10:30:00+00:00',
                'timeZone': 'UTC',
            },
            'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=TU'],
        }

    def get_event_resource(
        self, calendar_id: str, event_id: str
    ) -> dict[str, Any]:
        """Return recurring provider resource."""
        self.calls.append(('get', (calendar_id, event_id)))
        return dict(self.resource)

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        body: dict[str, Any],
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Record recurring series update."""
        self.calls.append(
            ('update', (calendar_id, event_id, body, etag, send_updates))
        )
        return EventMutationResult(original_series_updated=True)

    def create_event(
        self,
        calendar_id: str,
        body: dict[str, Any],
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Record following series creation."""
        self.calls.append(('create', (calendar_id, body, send_updates)))
        if self.fail_create:
            raise RuntimeError('provider marker')
        return EventMutationResult(new_series_created=True)


def test_future_update_splits_series_and_reports_partial_failure() -> None:
    gateway = FakeGateway()
    mutator = RecurringEventMutator(gateway)
    result = mutator.update_future(
        'primary',
        'series-1',
        '2026-09-01T10:00:00+00:00',
        {'summary': 'Changed'},
        'etag-1',
        SendUpdates.NONE,
    )
    assert result.original_series_updated is True
    assert result.new_series_created is True
    assert gateway.calls[1][1][2]['recurrence'][0].endswith(
        'UNTIL=20260901T095959Z'
    )
    assert gateway.calls[2][1][1]['summary'] == 'Changed'
    assert gateway.calls[2][1][1]['start']['dateTime'].startswith(
        '2026-09-01T10:00:00'
    )

    failed_gateway = FakeGateway(fail_create=True)
    failed = RecurringEventMutator(failed_gateway).update_future(
        'primary',
        'series-1',
        '2026-09-01T10:00:00+00:00',
        {},
        'etag-1',
        SendUpdates.NONE,
    )
    assert failed.original_series_updated is True
    assert failed.new_series_created is False
    assert failed.partial_error == 'Calendar following series creation failed'
