"""Test Calendar provider gateway."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import GoogleCredentials
from google_workspace_mcp.services.calendar.client import CalendarGateway
from google_workspace_mcp.services.calendar.errors import CalendarProviderError
from google_workspace_mcp.services.calendar.schemas import SendUpdates


class FakeRequest:
    """Record Calendar request execution."""

    def __init__(
        self, value: Any = None, error: Exception | None = None
    ) -> None:
        """Initialize Calendar request fake."""
        self.value = value
        self.error = error
        self.retries: list[int] = []
        self.headers: dict[str, str] = {}

    def execute(self, *, num_retries: int = 0) -> Any:
        """Execute Calendar request fake."""
        self.retries.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.value


class FakeEndpoint:
    """Record Calendar endpoint calls."""

    def __init__(self) -> None:
        """Initialize Calendar endpoint fake."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        """Queue Calendar endpoint values."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record Calendar endpoint request."""
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def list(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar list call."""
        return self._call('list', kwargs)

    def get(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar get call."""
        return self._call('get', kwargs)

    def instances(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar instances call."""
        return self._call('instances', kwargs)

    def insert(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar insert call."""
        return self._call('insert', kwargs)

    def update(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar update call."""
        return self._call('update', kwargs)

    def patch(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar patch call."""
        return self._call('patch', kwargs)

    def delete(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar delete call."""
        return self._call('delete', kwargs)

    def query(self, **kwargs: Any) -> FakeRequest:
        """Record Calendar query call."""
        return self._call('query', kwargs)


class FakeCalendarService:
    """Expose fake Calendar endpoints."""

    def __init__(self) -> None:
        """Initialize Calendar service fake."""
        self.calendars = FakeEndpoint()
        self.event_values = FakeEndpoint()
        self.availability = FakeEndpoint()

    def calendarList(self) -> FakeEndpoint:
        """Return Calendar list endpoint."""
        return self.calendars

    def events(self) -> FakeEndpoint:
        """Return Calendar events endpoint."""
        return self.event_values

    def freebusy(self) -> FakeEndpoint:
        """Return Calendar freebusy endpoint."""
        return self.availability


class FakeStore:
    """Return Calendar test credentials."""

    def __init__(self) -> None:
        """Initialize Calendar store fake."""
        self.calls = 0
        self.credentials = GoogleCredentials(
            token='calendar-provider-token',
            scopes=('https://www.googleapis.com/auth/calendar.events',),
        )

    def refresh(self) -> GoogleCredentials:
        """Return Calendar credentials."""
        self.calls += 1
        return self.credentials


def _event(event_id: str = 'event-1') -> dict[str, Any]:
    """Build Calendar event response."""
    return {
        'id': event_id,
        'etag': 'etag-1',
        'status': 'confirmed',
        'summary': 'Meeting',
        'start': {
            'dateTime': '2026-08-25T10:00:00+00:00',
            'timeZone': 'UTC',
        },
        'end': {
            'dateTime': '2026-08-25T11:00:00+00:00',
            'timeZone': 'UTC',
        },
    }


def test_calendar_list_and_event_search_use_native_retry() -> None:
    service = FakeCalendarService()
    store = FakeStore()
    service.calendars.queue(
        'list',
        {
            'items': [
                {'id': 'primary', 'summary': 'Primary', 'primary': True}
            ],
            'nextPageToken': 'calendar-next',
        },
    )
    service.event_values.queue(
        'list',
        {
            'items': [_event()],
            'nextPageToken': 'event-next',
            'timeZone': 'UTC',
        },
    )
    gateway = CalendarGateway(store, service_builder=lambda _: service)

    calendars = gateway.list_calendars(50, None)
    events = gateway.search_events(
        'primary',
        '2026-08-25T00:00:00Z',
        '2026-08-26T00:00:00Z',
        '',
        10,
        None,
        'UTC',
    )

    assert calendars.items[0].calendar_id == 'primary'
    assert calendars.next_page_token == 'calendar-next'
    assert events.items[0].event_id == 'event-1'
    assert events.next_page_token == 'event-next'
    assert service.event_values.calls[0][1] == {
        'calendarId': 'primary',
        'timeMin': '2026-08-25T00:00:00Z',
        'timeMax': '2026-08-26T00:00:00Z',
        'q': '',
        'singleEvents': True,
        'orderBy': 'startTime',
        'maxResults': 10,
        'timeZone': 'UTC',
    }
    assert all(
        request.retries == [2]
        for endpoint in (service.calendars, service.event_values)
        for _, _, request in endpoint.calls
    )
    assert store.calls == 2


def test_freebusy_preserves_partial_errors() -> None:
    service = FakeCalendarService()
    service.availability.queue(
        'query',
        {
            'timeMin': '2026-08-25T00:00:00Z',
            'timeMax': '2026-08-26T00:00:00Z',
            'calendars': {
                'primary': {
                    'busy': [
                        {
                            'start': '2026-08-25T10:00:00Z',
                            'end': '2026-08-25T11:00:00Z',
                        }
                    ]
                },
                'missing': {'errors': [{'reason': 'notFound'}]},
            },
        },
    )
    gateway = CalendarGateway(FakeStore(), service_builder=lambda _: service)
    result = gateway.get_freebusy(
        ('primary', 'missing'),
        '2026-08-25T00:00:00Z',
        '2026-08-26T00:00:00Z',
        'UTC',
    )
    assert result.calendars[0].busy[0].start.endswith('Z')
    assert result.calendars[1].errors[0].reason == 'notFound'
    assert (
        service.availability.calls[0][1]['body']['calendarExpansionMax'] == 50
    )


@pytest.mark.parametrize('missing_key', ['items', 'nextPageToken'])
def test_calendar_collections_normalize_missing_keys(missing_key: str) -> None:
    del missing_key
    service = FakeCalendarService()
    service.calendars.queue('list', {})
    gateway = CalendarGateway(FakeStore(), service_builder=lambda _: service)
    result = gateway.list_calendars(10, None)
    assert result.items == ()
    assert result.next_page_token is None


def test_event_mutations_bind_etag_and_send_updates() -> None:
    service = FakeCalendarService()
    service.event_values.queue('insert', _event('created'))
    service.event_values.queue('patch', _event('event-1'))
    service.event_values.queue('delete', None)
    gateway = CalendarGateway(FakeStore(), service_builder=lambda _: service)
    body = {
        'summary': 'Meeting',
        'start': {'dateTime': '2026-08-25T10:00:00Z', 'timeZone': 'UTC'},
        'end': {'dateTime': '2026-08-25T11:00:00Z', 'timeZone': 'UTC'},
    }

    assert (
        gateway.create_event('primary', body, SendUpdates.NONE).event
        is not None
    )
    patched = gateway.patch_event(
        'primary', 'event-1', {'summary': 'Changed'}, 'etag-1', SendUpdates.ALL
    )
    deleted = gateway.delete_event(
        'primary', 'event-1', 'etag-2', SendUpdates.EXTERNAL_ONLY
    )

    assert patched.event is not None
    assert deleted.deleted is True
    assert service.event_values.calls[1][2].headers['If-Match'] == 'etag-1'
    assert service.event_values.calls[2][2].headers['If-Match'] == 'etag-2'
    assert service.event_values.calls[1][1]['sendUpdates'] == 'all'
    assert service.event_values.calls[2][1]['sendUpdates'] == 'externalOnly'


def test_calendar_provider_error_is_sanitized() -> None:
    marker = 'calendar-provider-secret'
    response = httplib2.Response({'status': '403'})
    error = HttpError(
        response,
        ('{"error":{"message":"' + marker + '"}}').encode(),
        uri='https://calendar.googleapis.test?q=' + marker,
    )
    service = FakeCalendarService()
    service.event_values.queue('list', FakeRequest(error=error))
    gateway = CalendarGateway(FakeStore(), service_builder=lambda _: service)
    with pytest.raises(CalendarProviderError) as captured:
        gateway.search_events(
            'primary',
            '2026-08-25T00:00:00Z',
            '2026-08-26T00:00:00Z',
            marker,
            10,
            None,
            'UTC',
        )
    assert marker not in str(captured.value)
    assert str(captured.value) == 'Calendar request was forbidden'
