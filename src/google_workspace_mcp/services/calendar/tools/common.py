"""Support safe Calendar tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from email.utils import getaddresses
from typing import cast

from ..errors import CalendarError, CalendarInputError, CalendarProviderError
from ..schemas import EventDate, EventDateTime, ReminderOverride
from ..time import all_day_range, normalize_time_range


async def run_gateway[ResultT](
    operation: Callable[..., ResultT], *args: object
) -> ResultT:
    """Execute scrubbed Calendar operation."""
    try:
        return await asyncio.to_thread(operation, *args)
    except CalendarError:
        raise
    except Exception:
        raise CalendarProviderError(
            'Calendar returned an invalid response'
        ) from None


def _mailbox(value: str) -> str:
    """Validate Calendar attendee mailbox."""
    if not value or len(value) > 320 or any(ord(char) < 32 for char in value):
        raise CalendarInputError('attendee email is invalid')
    parsed = getaddresses([value], strict=True)
    if len(parsed) != 1:
        raise CalendarInputError('attendee email is invalid')
    address = parsed[0][1]
    local, separator, domain = address.rpartition('@')
    if separator != '@' or not local or not domain or '..' in address:
        raise CalendarInputError('attendee email is invalid')
    return address


def build_event_body(
    summary: str,
    description: str | None,
    location: str | None,
    start_datetime: str | None,
    end_datetime: str | None,
    time_zone: str | None,
    start_date: str | None,
    end_date: str | None,
    attendees: Sequence[str] | None,
    reminders_use_default: bool | None,
    reminder_overrides: Sequence[ReminderOverride] | None,
    recurrence: Sequence[str] | None,
    *,
    partial: bool,
) -> dict[str, object]:
    """Build validated Calendar body."""
    body: dict[str, object] = {}
    if summary or not partial:
        if not summary.strip():
            raise CalendarInputError('event summary is required')
        body['summary'] = summary.strip()
    if description is not None:
        body['description'] = description
    if location is not None:
        body['location'] = location
    timed_values = (start_datetime, end_datetime, time_zone)
    all_day_values = (start_date, end_date)
    has_timed = any(value is not None for value in timed_values)
    has_all_day = any(value is not None for value in all_day_values)
    if has_timed and has_all_day:
        raise CalendarInputError('timed and all day boundaries cannot mix')
    if has_timed:
        if not all(timed_values):
            raise CalendarInputError(
                'timed event requires start, end and timezone'
            )
        normalized = normalize_time_range(
            str(start_datetime), str(end_datetime), str(time_zone)
        )
        timed_start = cast(EventDateTime, normalized.start)
        timed_end = cast(EventDateTime, normalized.end)
        body['start'] = {
            'dateTime': timed_start.date_time,
            'timeZone': timed_start.time_zone,
        }
        body['end'] = {
            'dateTime': timed_end.date_time,
            'timeZone': timed_end.time_zone,
        }
    elif has_all_day:
        if not all(all_day_values):
            raise CalendarInputError(
                'all day event requires start and end dates'
            )
        normalized = all_day_range(str(start_date), str(end_date))
        all_day_start = cast(EventDate, normalized.start)
        all_day_end = cast(EventDate, normalized.end)
        body['start'] = {'date': all_day_start.date.isoformat()}
        body['end'] = {'date': all_day_end.date.isoformat()}
    elif not partial:
        raise CalendarInputError('event boundaries are required')
    if attendees is not None:
        if len(attendees) > 100:
            raise CalendarInputError('too many attendees')
        body['attendees'] = [{'email': _mailbox(value)} for value in attendees]
    if reminders_use_default is not None or reminder_overrides is not None:
        overrides = tuple(reminder_overrides or ())
        if len(overrides) > 5:
            raise CalendarInputError('too many reminder overrides')
        if reminders_use_default and overrides:
            raise CalendarInputError(
                'default reminders cannot include overrides'
            )
        body['reminders'] = {
            'useDefault': bool(reminders_use_default),
            'overrides': [value.model_dump() for value in overrides],
        }
    if recurrence is not None:
        if len(recurrence) > 10:
            raise CalendarInputError('too many recurrence lines')
        values = tuple(value.strip() for value in recurrence)
        if any(
            not value.startswith(('RRULE:', 'RDATE:', 'EXDATE:'))
            for value in values
        ):
            raise CalendarInputError('recurrence line is invalid')
        if values and has_timed and not time_zone:
            raise CalendarInputError('timed recurrence requires timezone')
        body['recurrence'] = list(values)
    return body
