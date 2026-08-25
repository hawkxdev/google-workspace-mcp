"""Register Calendar event tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import CalendarGateway
from ..constants import MAX_ATTENDEES, MAX_RECURRENCE_LINES
from ..errors import CalendarInputError
from ..recurrence import RecurringEventMutator
from ..schemas import (
    EventMutationResult,
    RecurrenceScope,
    ReminderOverride,
    SendUpdates,
)
from .common import build_event_body, run_gateway

AttendeeInput = Annotated[str, Field(min_length=3, max_length=320)]
RecurrenceInput = Annotated[str, Field(min_length=1, max_length=1_000)]


def register_event_tools(
    registrar: ToolRegistrar,
    gateway: CalendarGateway,
    recurring: RecurringEventMutator,
) -> None:
    """Register Calendar event tools."""

    @registrar.tool(
        name='calendar_create_event',
        title='Create Calendar Event',
        description='Create one timed or all-day calendar event.',
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def calendar_create_event(
        calendar_id: Annotated[str, Field(min_length=1, max_length=256)],
        summary: Annotated[str, Field(min_length=1, max_length=4000)],
        description: Annotated[str | None, Field(max_length=4000)] = None,
        location: Annotated[str | None, Field(max_length=4000)] = None,
        start_datetime: Annotated[str | None, Field(max_length=128)] = None,
        end_datetime: Annotated[str | None, Field(max_length=128)] = None,
        time_zone: Annotated[str | None, Field(max_length=128)] = None,
        start_date: Annotated[str | None, Field(max_length=10)] = None,
        end_date: Annotated[str | None, Field(max_length=10)] = None,
        attendees: Annotated[
            list[AttendeeInput] | None, Field(max_length=MAX_ATTENDEES)
        ] = None,
        reminders_use_default: bool | None = None,
        reminder_overrides: Annotated[
            list[ReminderOverride] | None, Field(max_length=5)
        ] = None,
        recurrence: Annotated[
            list[RecurrenceInput] | None,
            Field(max_length=MAX_RECURRENCE_LINES),
        ] = None,
        send_updates: SendUpdates = SendUpdates.NONE,
    ) -> EventMutationResult:
        """Create one Calendar event."""
        body = build_event_body(
            summary,
            description,
            location,
            start_datetime,
            end_datetime,
            time_zone,
            start_date,
            end_date,
            attendees,
            reminders_use_default,
            reminder_overrides,
            recurrence,
            partial=False,
        )
        return await run_gateway(
            gateway.create_event, calendar_id, body, send_updates
        )

    @registrar.tool(
        name='calendar_update_event',
        title='Update Calendar Event',
        description='Update one occurrence, series, or future events.',
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def calendar_update_event(
        calendar_id: Annotated[str, Field(min_length=1, max_length=256)],
        event_id: Annotated[str, Field(min_length=1, max_length=256)],
        etag: Annotated[str, Field(min_length=1, max_length=256)],
        recurrence_scope: RecurrenceScope = RecurrenceScope.SINGLE,
        occurrence_start: Annotated[str | None, Field(max_length=128)] = None,
        summary: Annotated[str | None, Field(max_length=4000)] = None,
        description: Annotated[str | None, Field(max_length=4000)] = None,
        location: Annotated[str | None, Field(max_length=4000)] = None,
        start_datetime: Annotated[str | None, Field(max_length=128)] = None,
        end_datetime: Annotated[str | None, Field(max_length=128)] = None,
        time_zone: Annotated[str | None, Field(max_length=128)] = None,
        start_date: Annotated[str | None, Field(max_length=10)] = None,
        end_date: Annotated[str | None, Field(max_length=10)] = None,
        attendees: Annotated[
            list[AttendeeInput] | None, Field(max_length=MAX_ATTENDEES)
        ] = None,
        reminders_use_default: bool | None = None,
        reminder_overrides: Annotated[
            list[ReminderOverride] | None, Field(max_length=5)
        ] = None,
        recurrence: Annotated[
            list[RecurrenceInput] | None,
            Field(max_length=MAX_RECURRENCE_LINES),
        ] = None,
        send_updates: SendUpdates = SendUpdates.NONE,
    ) -> EventMutationResult:
        """Update scoped Calendar event."""
        body = build_event_body(
            summary or '',
            description,
            location,
            start_datetime,
            end_datetime,
            time_zone,
            start_date,
            end_date,
            attendees,
            reminders_use_default,
            reminder_overrides,
            recurrence,
            partial=True,
        )
        if not body:
            raise CalendarInputError(
                'event update requires at least one change'
            )
        if recurrence_scope is RecurrenceScope.FUTURE:
            if not occurrence_start:
                raise CalendarInputError(
                    'future recurrence update requires occurrence start'
                )
            return await run_gateway(
                recurring.update_future,
                calendar_id,
                event_id,
                occurrence_start,
                body,
                etag,
                send_updates,
            )
        return await run_gateway(
            gateway.patch_event,
            calendar_id,
            event_id,
            body,
            etag,
            send_updates,
        )

    @registrar.tool(
        name='calendar_delete_event',
        title='Delete Calendar Event',
        description='Delete one occurrence, series, or future events.',
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def calendar_delete_event(
        calendar_id: Annotated[str, Field(min_length=1, max_length=256)],
        event_id: Annotated[str, Field(min_length=1, max_length=256)],
        etag: Annotated[str, Field(min_length=1, max_length=256)],
        recurrence_scope: RecurrenceScope = RecurrenceScope.SINGLE,
        occurrence_start: Annotated[str | None, Field(max_length=128)] = None,
        send_updates: SendUpdates = SendUpdates.NONE,
    ) -> EventMutationResult:
        """Delete scoped Calendar event."""
        if recurrence_scope is RecurrenceScope.FUTURE:
            if not occurrence_start:
                raise CalendarInputError(
                    'future recurrence delete requires occurrence start'
                )
            return await run_gateway(
                recurring.delete_future,
                calendar_id,
                event_id,
                occurrence_start,
                etag,
                send_updates,
            )
        return await run_gateway(
            gateway.delete_event,
            calendar_id,
            event_id,
            etag,
            send_updates,
        )
