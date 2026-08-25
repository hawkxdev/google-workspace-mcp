"""Validate Calendar time values."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import MAX_WINDOW_DAYS
from .errors import CalendarInputError
from .schemas import EventDate, EventDateTime, EventTimeRange

_RFC3339_PATTERN = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
    r'(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$'
)


def _aware_datetime(value: str) -> datetime:
    """Parse offset aware timestamp."""
    if _RFC3339_PATTERN.fullmatch(value) is None:
        raise CalendarInputError('timestamp must be valid RFC3339')
    normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise CalendarInputError('timestamp must be valid RFC3339') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalendarInputError('timestamp must be offset-aware')
    return parsed


def validate_time_zone(value: str) -> ZoneInfo:
    """Load explicit IANA timezone."""
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise CalendarInputError(
            'time zone must be an IANA timezone'
        ) from None


def normalize_time_range(
    start: str,
    end: str,
    time_zone: str,
    *,
    max_days: int = MAX_WINDOW_DAYS,
) -> EventTimeRange:
    """Normalize timed Calendar range."""
    zone = validate_time_zone(time_zone)
    start_value = _aware_datetime(start)
    end_value = _aware_datetime(end)
    for candidate in (start_value, end_value):
        localized = candidate.astimezone(zone)
        if localized.utcoffset() != candidate.utcoffset():
            raise CalendarInputError(
                'timestamp timezone offset is inconsistent'
            )
    if end_value <= start_value:
        raise CalendarInputError('event end must be after start')
    if end_value - start_value > timedelta(days=max_days):
        raise CalendarInputError('time window is too large')
    return EventTimeRange(
        start=EventDateTime(date_time=start, time_zone=time_zone),
        end=EventDateTime(date_time=end, time_zone=time_zone),
        all_day=False,
    )


def all_day_range(start: str, end: str) -> EventTimeRange:
    """Normalize all day range."""
    try:
        start_value = date.fromisoformat(start)
        end_value = date.fromisoformat(end)
    except ValueError:
        raise CalendarInputError('all day value must be an ISO date') from None
    if end_value <= start_value:
        raise CalendarInputError('all day end must be after start')
    if end_value - start_value > timedelta(days=MAX_WINDOW_DAYS):
        raise CalendarInputError('time window is too large')
    return EventTimeRange(
        start=EventDate(date=start_value),
        end=EventDate(date=end_value),
        all_day=True,
    )


def normalize_search_window(start: str, end: str) -> tuple[str, str]:
    """Normalize bounded search window."""
    start_value = _aware_datetime(start)
    end_value = _aware_datetime(end)
    if end_value <= start_value:
        raise CalendarInputError('time max must be after time min')
    if end_value - start_value > timedelta(days=MAX_WINDOW_DAYS):
        raise CalendarInputError('time window is too large')
    return start, end
