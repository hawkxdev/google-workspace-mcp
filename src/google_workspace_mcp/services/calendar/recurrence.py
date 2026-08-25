"""Apply Calendar recurrence mutations."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from .errors import CalendarInputError
from .schemas import EventMutationResult, SendUpdates


class RecurrenceGateway(Protocol):
    """Describe recurrence gateway methods."""

    def get_event_resource(
        self, calendar_id: str, event_id: str
    ) -> Mapping[str, Any]:
        """Return raw event resource."""
        ...

    def instance_exists(
        self, calendar_id: str, event_id: str, occurrence_start: str
    ) -> bool:
        """Check target event instance."""
        ...

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        body: Mapping[str, Any],
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Update one event resource."""
        ...

    def create_event(
        self,
        calendar_id: str,
        body: Mapping[str, Any],
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Create one event resource."""
        ...


def _occurrence_until(value: str) -> str:
    """Build recurrence UNTIL boundary."""
    if 'T' not in value:
        try:
            occurrence = date.fromisoformat(value)
        except ValueError:
            raise CalendarInputError('occurrence start is invalid') from None
        return (occurrence - timedelta(days=1)).strftime('%Y%m%d')
    normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        occurrence_time = datetime.fromisoformat(normalized)
    except ValueError:
        raise CalendarInputError('occurrence start is invalid') from None
    if occurrence_time.tzinfo is None:
        raise CalendarInputError('occurrence start must be offset-aware')
    boundary = occurrence_time.astimezone(UTC) - timedelta(seconds=1)
    return boundary.strftime('%Y%m%dT%H%M%SZ')


def split_recurrence(
    recurrence: Sequence[str], occurrence_start: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split recurring Calendar rule."""
    rules = tuple(recurrence)
    additional = tuple(
        value
        for value in rules
        if value.partition(':')[0].partition(';')[0].upper()
        in {'RDATE', 'EXDATE'}
    )
    if additional:
        raise CalendarInputError(
            'RDATE and EXDATE recurrence cannot be split safely'
        )
    rrules = [value for value in rules if value.startswith('RRULE:')]
    if len(rrules) != 1 or len(rules) != 1:
        raise CalendarInputError('one RRULE is required for future mutation')
    rule = rrules[0]
    if 'COUNT=' in rule.upper():
        raise CalendarInputError('COUNT recurrence cannot be split safely')
    until = _occurrence_until(occurrence_start)
    components = [
        value
        for value in rule.removeprefix('RRULE:').split(';')
        if value and not value.upper().startswith('UNTIL=')
    ]
    original_rule = f'RRULE:{";".join((*components, f"UNTIL={until}"))}'
    return (original_rule,), rules


def _shift_start_end(resource: dict[str, Any], occurrence_start: str) -> None:
    """Shift following series boundaries."""
    start = resource.get('start')
    end = resource.get('end')
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise CalendarInputError('recurring event boundaries are invalid')
    if 'dateTime' in start and 'dateTime' in end:
        original_start = datetime.fromisoformat(
            str(start['dateTime']).replace('Z', '+00:00')
        )
        original_end = datetime.fromisoformat(
            str(end['dateTime']).replace('Z', '+00:00')
        )
        target = datetime.fromisoformat(
            occurrence_start.replace('Z', '+00:00')
        )
        if (
            original_start.tzinfo is None
            or original_end.tzinfo is None
            or target.tzinfo is None
        ):
            raise CalendarInputError('recurring time must be offset-aware')
        duration = original_end - original_start
        shifted_end = target + duration
        resource['start'] = {
            'dateTime': occurrence_start,
            'timeZone': str(start.get('timeZone', '')),
        }
        resource['end'] = {
            'dateTime': shifted_end.isoformat(),
            'timeZone': str(end.get('timeZone', start.get('timeZone', ''))),
        }
        return
    if 'date' in start and 'date' in end:
        original_start_date = date.fromisoformat(str(start['date']))
        original_end_date = date.fromisoformat(str(end['date']))
        target_date = date.fromisoformat(occurrence_start)
        duration = original_end_date - original_start_date
        resource['start'] = {'date': target_date.isoformat()}
        resource['end'] = {'date': (target_date + duration).isoformat()}
        return
    raise CalendarInputError('recurring event boundary types differ')


def _writable_resource(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy writable event fields."""
    resource = copy.deepcopy(dict(value))
    for field in (
        'id',
        'etag',
        'htmlLink',
        'created',
        'updated',
        'creator',
        'organizer',
        'iCalUID',
        'sequence',
        'kind',
    ):
        resource.pop(field, None)
    return resource


class RecurringEventMutator:
    """Apply recurring Calendar changes."""

    def __init__(self, gateway: RecurrenceGateway) -> None:
        """Initialize recurrence mutator."""
        self._gateway = gateway

    def update_future(
        self,
        calendar_id: str,
        series_id: str,
        occurrence_start: str,
        changes: Mapping[str, Any],
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Update future recurring series."""
        resource = self._gateway.get_event_resource(calendar_id, series_id)
        original_body = _writable_resource(resource)
        recurrence = original_body.get('recurrence')
        if not isinstance(recurrence, Sequence) or isinstance(
            recurrence, str | bytes
        ):
            raise CalendarInputError('event is not a recurring series')
        if not self._gateway.instance_exists(
            calendar_id, series_id, occurrence_start
        ):
            raise CalendarInputError(
                'future recurrence target instance not found'
            )
        original_recurrence, following_recurrence = split_recurrence(
            tuple(str(value) for value in recurrence), occurrence_start
        )
        following_body = _writable_resource(resource)
        following_body.update(dict(changes))
        custom_recurrence = changes.get('recurrence')
        if custom_recurrence is None:
            following_body['recurrence'] = list(following_recurrence)
        elif not isinstance(custom_recurrence, Sequence) or isinstance(
            custom_recurrence, str | bytes
        ):
            raise CalendarInputError('future recurrence rules are invalid')
        else:
            following_body['recurrence'] = [
                str(value) for value in custom_recurrence
            ]
        changes_time = 'start' in changes or 'end' in changes
        if changes_time and not {'start', 'end'}.issubset(changes):
            raise CalendarInputError(
                'future recurrence time change requires start and end'
            )
        if not changes_time:
            _shift_start_end(following_body, occurrence_start)
        original_body['recurrence'] = list(original_recurrence)
        self._gateway.update_event(
            calendar_id,
            series_id,
            original_body,
            etag,
            send_updates,
        )
        try:
            created = self._gateway.create_event(
                calendar_id, following_body, send_updates
            )
        except Exception:
            return EventMutationResult(
                original_series_updated=True,
                new_series_created=False,
                partial_error='Calendar following series creation failed',
            )
        return EventMutationResult(
            event=created.event,
            original_series_updated=True,
            new_series_created=True,
        )

    def delete_future(
        self,
        calendar_id: str,
        series_id: str,
        occurrence_start: str,
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Truncate future recurring series."""
        resource = self._gateway.get_event_resource(calendar_id, series_id)
        body = _writable_resource(resource)
        recurrence = body.get('recurrence')
        if not isinstance(recurrence, Sequence) or isinstance(
            recurrence, str | bytes
        ):
            raise CalendarInputError('event is not a recurring series')
        if not self._gateway.instance_exists(
            calendar_id, series_id, occurrence_start
        ):
            raise CalendarInputError(
                'future recurrence target instance not found'
            )
        original, _ = split_recurrence(
            tuple(str(value) for value in recurrence), occurrence_start
        )
        body['recurrence'] = list(original)
        updated = self._gateway.update_event(
            calendar_id, series_id, body, etag, send_updates
        )
        return EventMutationResult(
            event=updated.event,
            original_series_updated=True,
        )
