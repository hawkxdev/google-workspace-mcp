"""Define Calendar service schemas."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CalendarModel(BaseModel):
    """Configure Calendar schema model."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class RecurrenceScope(StrEnum):
    """Select recurring mutation scope."""

    SINGLE = 'single'
    SERIES = 'series'
    FUTURE = 'future'


class SendUpdates(StrEnum):
    """Select attendee notification policy."""

    NONE = 'none'
    ALL = 'all'
    EXTERNAL_ONLY = 'externalOnly'


class BatchOperationType(StrEnum):
    """Select Calendar batch operation."""

    CREATE = 'create'
    UPDATE = 'update'
    DELETE = 'delete'


class EventDateTime(CalendarModel):
    """Describe timed Calendar boundary."""

    date_time: str
    time_zone: str


class EventDate(CalendarModel):
    """Describe all day boundary."""

    date: date


class EventTimeRange(CalendarModel):
    """Describe Calendar event range."""

    start: EventDateTime | EventDate
    end: EventDateTime | EventDate
    all_day: bool


class Attendee(CalendarModel):
    """Describe Calendar event attendee."""

    email: str
    display_name: str = ''
    response_status: str = ''
    optional: bool = False


class ReminderOverride(CalendarModel):
    """Describe Calendar reminder override."""

    method: Literal['email', 'popup']
    minutes: int = Field(ge=0, le=40_320)


class CalendarSummary(CalendarModel):
    """Summarize Calendar list entry."""

    calendar_id: str
    summary: str = ''
    description: str = ''
    time_zone: str = ''
    primary: bool = False
    access_role: str = ''


class CalendarListResponse(CalendarModel):
    """Return paged Calendar entries."""

    items: tuple[CalendarSummary, ...] = ()
    next_page_token: str | None = None


class EventSummary(CalendarModel):
    """Summarize Calendar event."""

    event_id: str
    calendar_id: str
    etag: str = ''
    status: str = ''
    summary: str = ''
    description: str = ''
    location: str = ''
    start: EventDateTime | EventDate
    end: EventDateTime | EventDate
    html_link: str = ''
    recurring_event_id: str | None = None
    original_start: EventDateTime | EventDate | None = None


class EventDetail(EventSummary):
    """Describe Calendar event detail."""

    attendees: tuple[Attendee, ...] = ()
    reminders_use_default: bool = True
    reminder_overrides: tuple[ReminderOverride, ...] = ()
    recurrence: tuple[str, ...] = ()


class EventListResponse(CalendarModel):
    """Return paged Calendar events."""

    items: tuple[EventSummary, ...] = ()
    next_page_token: str | None = None
    time_zone: str = ''


class BusyPeriod(CalendarModel):
    """Describe Calendar busy period."""

    start: str
    end: str


class FreeBusyError(CalendarModel):
    """Describe Calendar availability error."""

    reason: str


class FreeBusyCalendar(CalendarModel):
    """Describe one Calendar availability."""

    calendar_id: str
    busy: tuple[BusyPeriod, ...] = ()
    errors: tuple[FreeBusyError, ...] = ()


class FreeBusyResponse(CalendarModel):
    """Return Calendar availability results."""

    time_min: str
    time_max: str
    calendars: tuple[FreeBusyCalendar, ...] = ()
    group_errors: tuple[FreeBusyError, ...] = ()


class EventMutationResult(CalendarModel):
    """Describe Calendar event mutation."""

    event: EventDetail | None = None
    event_id: str = ''
    deleted: bool = False
    original_series_updated: bool = False
    new_series_created: bool = False
    partial_error: str | None = None


class BatchOperation(CalendarModel):
    """Describe Calendar batch mutation."""

    operation_id: str
    operation: BatchOperationType
    calendar_id: str
    event_id: str | None = None
    etag: str | None = None
    body: dict[str, object] | None = None
    send_updates: SendUpdates = SendUpdates.NONE


class BatchItemResult(CalendarModel):
    """Describe Calendar batch item."""

    operation_id: str
    success: bool
    event_id: str | None = None
    deleted: bool = False
    error: str | None = None


class BatchMutationResponse(CalendarModel):
    """Return Calendar batch results."""

    items: tuple[BatchItemResult, ...]
