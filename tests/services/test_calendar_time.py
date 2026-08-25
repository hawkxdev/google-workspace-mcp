"""Test Calendar time semantics."""

from __future__ import annotations

from datetime import date

import pytest

from google_workspace_mcp.services.calendar.errors import CalendarInputError
from google_workspace_mcp.services.calendar.schemas import ReminderOverride
from google_workspace_mcp.services.calendar.time import (
    all_day_range,
    normalize_time_range,
)
from google_workspace_mcp.services.calendar.tools.common import (
    build_event_body,
)


def test_timed_range_requires_offset_and_iana_zone() -> None:
    normalized = normalize_time_range(
        '2026-11-01T01:30:00-04:00',
        '2026-11-01T02:30:00-05:00',
        'America/New_York',
    )
    assert normalized.start.date_time == '2026-11-01T01:30:00-04:00'
    assert normalized.end.date_time == '2026-11-01T02:30:00-05:00'
    assert normalized.start.time_zone == 'America/New_York'

    with pytest.raises(CalendarInputError, match='offset-aware'):
        normalize_time_range(
            '2026-08-25T10:00:00',
            '2026-08-25T11:00:00',
            'UTC',
        )
    with pytest.raises(CalendarInputError, match='IANA timezone'):
        normalize_time_range(
            '2026-08-25T10:00:00Z',
            '2026-08-25T11:00:00Z',
            'Not/AZone',
        )


def test_timed_range_rejects_nonexistent_dst_time() -> None:
    with pytest.raises(CalendarInputError, match='timezone offset'):
        normalize_time_range(
            '2026-03-08T02:30:00-05:00',
            '2026-03-08T03:30:00-04:00',
            'America/New_York',
        )


def test_all_day_end_is_exclusive() -> None:
    normalized = all_day_range('2026-08-25', '2026-08-26')
    assert normalized.start.date == date(2026, 8, 25)
    assert normalized.end.date == date(2026, 8, 26)
    with pytest.raises(CalendarInputError, match='after start'):
        all_day_range('2026-08-25', '2026-08-25')


def test_event_body_validates_attendees_and_reminders() -> None:
    body = build_event_body(
        'Planning',
        None,
        None,
        None,
        None,
        None,
        '2026-08-25',
        '2026-08-26',
        ('alice@example.com',),
        False,
        (ReminderOverride(method='popup', minutes=10),),
        None,
        partial=False,
    )
    assert body['attendees'] == [{'email': 'alice@example.com'}]
    assert body['reminders']['overrides'] == [
        {'method': 'popup', 'minutes': 10}
    ]

    with pytest.raises(CalendarInputError, match='recipient|attendee'):
        build_event_body(
            'Planning',
            None,
            None,
            None,
            None,
            None,
            '2026-08-25',
            '2026-08-26',
            ('not-an-email',),
            None,
            None,
            None,
            partial=False,
        )
    with pytest.raises(CalendarInputError, match='default reminders'):
        build_event_body(
            'Planning',
            None,
            None,
            None,
            None,
            None,
            '2026-08-25',
            '2026-08-26',
            None,
            True,
            (ReminderOverride(method='email', minutes=10),),
            None,
            partial=False,
        )
