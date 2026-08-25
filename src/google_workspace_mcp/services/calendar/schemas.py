"""Define Calendar service schemas."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

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


class FreeBusyGroup(CalendarModel):
    """Describe Calendar availability group."""

    group_id: str
    calendars: tuple[str, ...] = ()
    errors: tuple[FreeBusyError, ...] = ()


class FreeBusyResponse(CalendarModel):
    """Return Calendar availability results."""

    time_min: str
    time_max: str
    calendars: tuple[FreeBusyCalendar, ...] = ()
    groups: tuple[FreeBusyGroup, ...] = ()


class EventMutationResult(CalendarModel):
    """Describe Calendar event mutation."""

    event: EventDetail | None = None
    event_id: str = ''
    deleted: bool = False
    original_series_updated: bool = False
    new_series_created: bool = False
    partial_error: str | None = None


class BatchEventDateTime(CalendarModel):
    """Validate batch timed boundary."""

    date_time: str = Field(alias='dateTime', min_length=1, max_length=128)
    time_zone: str = Field(alias='timeZone', min_length=1, max_length=128)


class BatchEventDate(CalendarModel):
    """Validate batch date boundary."""

    date: str = Field(min_length=10, max_length=10)


class BatchAttendee(CalendarModel):
    """Validate batch event attendee."""

    email: str = Field(min_length=3, max_length=320)


class BatchReminders(CalendarModel):
    """Validate batch event reminders."""

    use_default: bool = Field(alias='useDefault')
    overrides: tuple[ReminderOverride, ...] = Field(max_length=5)


class BatchEventBody(CalendarModel):
    """Validate batch event body."""

    summary: str | None = Field(default=None, max_length=4_000)
    description: str | None = Field(default=None, max_length=4_000)
    location: str | None = Field(default=None, max_length=4_000)
    start: BatchEventDateTime | BatchEventDate | None = None
    end: BatchEventDateTime | BatchEventDate | None = None
    attendees: tuple[BatchAttendee, ...] | None = Field(
        default=None, max_length=100
    )
    reminders: BatchReminders | None = None
    recurrence: (
        tuple[
            Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=1_000,
                    pattern=r'^(?:RRULE|RDATE|EXDATE):',
                ),
            ],
            ...,
        ]
        | None
    ) = Field(default=None, max_length=10)


class BatchOperation(CalendarModel):
    """Describe Calendar batch mutation."""

    operation_id: str = Field(min_length=1, max_length=256)
    operation: BatchOperationType
    calendar_id: str = Field(min_length=1, max_length=256)
    event_id: str | None = Field(default=None, min_length=1, max_length=256)
    etag: str | None = Field(default=None, min_length=1, max_length=256)
    body: BatchEventBody | None = None
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
