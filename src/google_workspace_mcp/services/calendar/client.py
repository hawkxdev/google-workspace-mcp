"""Call Calendar provider methods."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Literal, cast

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)

from .constants import (
    MAX_ATTENDEES,
    MAX_CALENDAR_PAGE_SIZE,
    MAX_EVENT_PAGE_SIZE,
    MAX_FREEBUSY_CALENDARS,
    MAX_ID_CHARS,
    MAX_RECURRENCE_LINES,
    MAX_REMINDERS,
    MAX_TEXT_CHARS,
    REQUEST_RETRIES,
)
from .errors import (
    CalendarConflictError,
    CalendarInputError,
    CalendarProviderError,
)
from .schemas import (
    Attendee,
    BusyPeriod,
    CalendarListResponse,
    CalendarSummary,
    EventDate,
    EventDateTime,
    EventDetail,
    EventListResponse,
    EventMutationResult,
    EventSummary,
    FreeBusyCalendar,
    FreeBusyError,
    FreeBusyResponse,
    ReminderOverride,
    SendUpdates,
)
from .time import normalize_search_window, validate_time_zone

ServiceBuilder = Callable[[GoogleCredentials], Any]


def build_calendar_service(credentials: GoogleCredentials) -> Any:
    """Build Calendar provider service."""
    return build(
        'calendar',
        'v3',
        credentials=credentials.to_google_credentials(),
        cache_discovery=False,
        static_discovery=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    """Require Calendar response mapping."""
    if not isinstance(value, Mapping):
        raise CalendarProviderError('Calendar returned an invalid response')
    return value


def _sequence(value: Any, limit: int) -> Sequence[Any]:
    """Require bounded Calendar collection."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise CalendarProviderError('Calendar returned an invalid response')
    if len(value) > limit:
        raise CalendarProviderError('Calendar returned an invalid response')
    return value


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """Normalize bounded Calendar text."""
    return str(value or '')[:limit]


def _event_time(value: Any) -> EventDateTime | EventDate:
    """Normalize Calendar event boundary."""
    data = _mapping(value)
    date_time = data.get('dateTime')
    if isinstance(date_time, str):
        return EventDateTime(
            date_time=date_time[:MAX_TEXT_CHARS],
            time_zone=_text(data.get('timeZone'), 128),
        )
    date_value = data.get('date')
    if isinstance(date_value, str):
        try:
            return EventDate(date=date.fromisoformat(date_value))
        except ValueError:
            pass
    raise CalendarProviderError('Calendar returned an invalid response')


def _safe_reason(value: Any) -> str:
    """Normalize Calendar error reason."""
    reason = _text(value, 128)
    return reason if reason else 'unknown'


def _reminder_method(value: Any) -> Literal['email', 'popup']:
    """Normalize Calendar reminder method."""
    method = _text(value, 16)
    if method not in {'email', 'popup'}:
        raise CalendarProviderError('Calendar returned an invalid response')
    return cast(Literal['email', 'popup'], method)


class CalendarGateway:
    """Normalize Calendar provider operations."""

    def __init__(
        self,
        store: GoogleCredentialStore,
        *,
        service_builder: ServiceBuilder = build_calendar_service,
        num_retries: int = REQUEST_RETRIES,
    ) -> None:
        """Initialize Calendar provider gateway."""
        self._store = store
        self._service_builder = service_builder
        self._num_retries = num_retries

    def service(self) -> Any:
        """Build authenticated Calendar service."""
        try:
            return self._service_builder(self._store.refresh())
        except CalendarProviderError:
            raise
        except Exception:
            raise CalendarProviderError(
                'Calendar credentials are unavailable'
            ) from None

    @staticmethod
    def _http_reason(error: HttpError) -> str | None:
        """Read safe Calendar reason."""
        try:
            content = json.loads(error.content.decode('utf-8'))
            errors = content.get('error', {}).get('errors', [])
            if isinstance(errors, list) and errors:
                reason = errors[0].get('reason')
                return reason if isinstance(reason, str) else None
        except AttributeError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        return None

    def execute_raw(self, request: Any) -> Any:
        """Execute raw Calendar request."""
        try:
            return request.execute(num_retries=self._num_retries)
        except HttpError as error:
            status = int(getattr(error.resp, 'status', 0))
            reason = self._http_reason(error)
            if status in {409, 412}:
                raise CalendarConflictError(
                    'Calendar event changed since it was read'
                ) from None
            if status in {403, 429} and reason in {
                'rateLimitExceeded',
                'userRateLimitExceeded',
            }:
                message = 'Calendar is temporarily rate limited'
            else:
                message = {
                    400: 'Calendar rejected the request',
                    401: 'Google authorization requires renewal',
                    403: 'Calendar request was forbidden',
                    404: 'Calendar resource was not found',
                    429: 'Calendar is temporarily rate limited',
                }.get(status, 'Calendar request is temporarily unavailable')
            raise CalendarProviderError(message) from None
        except TransportError, TimeoutError, ConnectionError, OSError:
            raise CalendarProviderError(
                'Calendar request is temporarily unavailable'
            ) from None

    def execute(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped Calendar request."""
        return _mapping(self.execute_raw(request))

    def execute_empty(self, request: Any) -> None:
        """Execute empty Calendar request."""
        value = self.execute_raw(request)
        if value not in (None, '', {}):
            raise CalendarProviderError(
                'Calendar returned an invalid response'
            )

    @staticmethod
    def _event(data: Mapping[str, Any], calendar_id: str) -> EventDetail:
        """Normalize Calendar event response."""
        attendees_value = data.get('attendees') or ()
        attendees = tuple(
            Attendee(
                email=_text(item.get('email'), 320),
                display_name=_text(item.get('displayName')),
                response_status=_text(item.get('responseStatus'), 64),
                optional=bool(item.get('optional', False)),
            )
            for item in (
                _mapping(value)
                for value in _sequence(attendees_value, MAX_ATTENDEES)
            )
        )
        reminders = _mapping(data.get('reminders') or {})
        overrides_value = reminders.get('overrides') or ()
        overrides = tuple(
            ReminderOverride(
                method=_reminder_method(item.get('method')),
                minutes=int(item.get('minutes', 0)),
            )
            for item in (
                _mapping(value)
                for value in _sequence(overrides_value, MAX_REMINDERS)
            )
        )
        recurrence_value = data.get('recurrence') or ()
        recurrence = tuple(
            _text(value, 1_000)
            for value in _sequence(recurrence_value, MAX_RECURRENCE_LINES)
        )
        original_value = data.get('originalStartTime')
        return EventDetail(
            event_id=_text(data.get('id'), MAX_ID_CHARS),
            calendar_id=calendar_id[:MAX_ID_CHARS],
            etag=_text(data.get('etag'), MAX_ID_CHARS),
            status=_text(data.get('status'), 64),
            summary=_text(data.get('summary')),
            description=_text(data.get('description')),
            location=_text(data.get('location')),
            start=_event_time(data.get('start')),
            end=_event_time(data.get('end')),
            html_link=_text(data.get('htmlLink'), 2_048),
            recurring_event_id=(
                _text(data.get('recurringEventId'), MAX_ID_CHARS) or None
            ),
            original_start=(
                _event_time(original_value) if original_value else None
            ),
            attendees=attendees,
            reminders_use_default=bool(reminders.get('useDefault', True)),
            reminder_overrides=overrides,
            recurrence=recurrence,
        )

    @staticmethod
    def _summary(event: EventDetail) -> EventSummary:
        """Build Calendar event summary."""
        return EventSummary(
            **event.model_dump(
                exclude={
                    'attendees',
                    'reminders_use_default',
                    'reminder_overrides',
                    'recurrence',
                }
            )
        )

    def list_calendars(
        self, page_size: int, page_token: str | None
    ) -> CalendarListResponse:
        """List Calendar entries."""
        if not 1 <= page_size <= MAX_CALENDAR_PAGE_SIZE:
            raise CalendarInputError('calendar page size is invalid')
        service = self.service()
        kwargs: dict[str, Any] = {'maxResults': page_size}
        if page_token:
            kwargs['pageToken'] = page_token
        data = self.execute(service.calendarList().list(**kwargs))
        items_value = data.get('items') or ()
        items = tuple(
            CalendarSummary(
                calendar_id=_text(item.get('id'), MAX_ID_CHARS),
                summary=_text(item.get('summary')),
                description=_text(item.get('description')),
                time_zone=_text(item.get('timeZone'), 128),
                primary=bool(item.get('primary', False)),
                access_role=_text(item.get('accessRole'), 64),
            )
            for item in (
                _mapping(value) for value in _sequence(items_value, page_size)
            )
        )
        return CalendarListResponse(
            items=items,
            next_page_token=_text(data.get('nextPageToken'), 2_048) or None,
        )

    def search_events(
        self,
        calendar_id: str,
        time_min: str,
        time_max: str,
        query: str,
        page_size: int,
        page_token: str | None,
        time_zone: str,
    ) -> EventListResponse:
        """Search bounded Calendar events."""
        validate_time_zone(time_zone)
        normalize_search_window(time_min, time_max)
        if not 1 <= page_size <= MAX_EVENT_PAGE_SIZE:
            raise CalendarInputError('event page size is invalid')
        service = self.service()
        kwargs: dict[str, Any] = {
            'calendarId': calendar_id,
            'timeMin': time_min,
            'timeMax': time_max,
            'q': query,
            'singleEvents': True,
            'orderBy': 'startTime',
            'maxResults': page_size,
            'timeZone': time_zone,
        }
        if page_token:
            kwargs['pageToken'] = page_token
        data = self.execute(service.events().list(**kwargs))
        values = data.get('items') or ()
        details = tuple(
            self._event(_mapping(value), calendar_id)
            for value in _sequence(values, page_size)
        )
        return EventListResponse(
            items=tuple(self._summary(value) for value in details),
            next_page_token=_text(data.get('nextPageToken'), 2_048) or None,
            time_zone=_text(data.get('timeZone'), 128),
        )

    def get_event(self, calendar_id: str, event_id: str) -> EventDetail:
        """Get one Calendar event."""
        service = self.service()
        data = self.execute(
            service.events().get(
                calendarId=calendar_id,
                eventId=event_id,
                maxAttendees=MAX_ATTENDEES,
            )
        )
        return self._event(data, calendar_id)

    def get_event_resource(
        self, calendar_id: str, event_id: str
    ) -> Mapping[str, Any]:
        """Get raw Calendar event."""
        service = self.service()
        return self.execute(
            service.events().get(
                calendarId=calendar_id,
                eventId=event_id,
                maxAttendees=MAX_ATTENDEES,
            )
        )

    def list_instances(
        self,
        calendar_id: str,
        recurring_event_id: str,
        time_min: str,
        time_max: str,
        page_size: int,
        page_token: str | None,
        time_zone: str,
    ) -> EventListResponse:
        """List recurring Calendar instances."""
        validate_time_zone(time_zone)
        normalize_search_window(time_min, time_max)
        if not 1 <= page_size <= MAX_EVENT_PAGE_SIZE:
            raise CalendarInputError('event page size is invalid')
        service = self.service()
        kwargs: dict[str, Any] = {
            'calendarId': calendar_id,
            'eventId': recurring_event_id,
            'timeMin': time_min,
            'timeMax': time_max,
            'maxResults': page_size,
            'timeZone': time_zone,
        }
        if page_token:
            kwargs['pageToken'] = page_token
        data = self.execute(service.events().instances(**kwargs))
        values = data.get('items') or ()
        details = tuple(
            self._event(_mapping(value), calendar_id)
            for value in _sequence(values, page_size)
        )
        return EventListResponse(
            items=tuple(self._summary(value) for value in details),
            next_page_token=_text(data.get('nextPageToken'), 2_048) or None,
            time_zone=_text(data.get('timeZone'), 128),
        )

    def get_freebusy(
        self,
        calendar_ids: Sequence[str],
        time_min: str,
        time_max: str,
        time_zone: str,
    ) -> FreeBusyResponse:
        """Get Calendar availability."""
        validate_time_zone(time_zone)
        normalize_search_window(time_min, time_max)
        if not 1 <= len(calendar_ids) <= MAX_FREEBUSY_CALENDARS:
            raise CalendarInputError('freebusy calendar count is invalid')
        service = self.service()
        body = {
            'timeMin': time_min,
            'timeMax': time_max,
            'timeZone': time_zone,
            'groupExpansionMax': 100,
            'calendarExpansionMax': MAX_FREEBUSY_CALENDARS,
            'items': [{'id': value} for value in calendar_ids],
        }
        data = self.execute(service.freebusy().query(body=body))
        calendars = _mapping(data.get('calendars', {}))
        output: list[FreeBusyCalendar] = []
        for calendar_id in calendar_ids:
            value = _mapping(calendars.get(calendar_id) or {})
            busy = tuple(
                BusyPeriod(
                    start=_text(item.get('start'), 128),
                    end=_text(item.get('end'), 128),
                )
                for item in (
                    _mapping(item)
                    for item in _sequence(value.get('busy') or (), 1_000)
                )
            )
            errors = tuple(
                FreeBusyError(reason=_safe_reason(item.get('reason')))
                for item in (
                    _mapping(item)
                    for item in _sequence(value.get('errors') or (), 10)
                )
            )
            output.append(
                FreeBusyCalendar(
                    calendar_id=calendar_id[:MAX_ID_CHARS],
                    busy=busy,
                    errors=errors,
                )
            )
        group_values = _mapping(data.get('groups') or {})
        group_errors = tuple(
            FreeBusyError(reason=_safe_reason(error.get('reason')))
            for group in group_values.values()
            for error in _sequence(_mapping(group).get('errors') or (), 10)
            if isinstance(error, Mapping)
        )
        return FreeBusyResponse(
            time_min=_text(data.get('timeMin'), 128),
            time_max=_text(data.get('timeMax'), 128),
            calendars=tuple(output),
            group_errors=group_errors,
        )

    def create_event(
        self,
        calendar_id: str,
        body: Mapping[str, Any],
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Create one Calendar event."""
        service = self.service()
        data = self.execute(
            service.events().insert(
                calendarId=calendar_id,
                body=dict(body),
                sendUpdates=send_updates.value,
            )
        )
        return EventMutationResult(event=self._event(data, calendar_id))

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        body: Mapping[str, Any],
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Update one Calendar event."""
        service = self.service()
        request = service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=dict(body),
            sendUpdates=send_updates.value,
        )
        request.headers['If-Match'] = etag
        data = self.execute(request)
        return EventMutationResult(event=self._event(data, calendar_id))

    def patch_event(
        self,
        calendar_id: str,
        event_id: str,
        body: Mapping[str, Any],
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Patch one Calendar event."""
        service = self.service()
        request = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=dict(body),
            sendUpdates=send_updates.value,
        )
        request.headers['If-Match'] = etag
        data = self.execute(request)
        return EventMutationResult(event=self._event(data, calendar_id))

    def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        etag: str,
        send_updates: SendUpdates,
    ) -> EventMutationResult:
        """Delete one Calendar event."""
        service = self.service()
        request = service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates.value,
        )
        request.headers['If-Match'] = etag
        self.execute_empty(request)
        return EventMutationResult(event_id=event_id, deleted=True)
