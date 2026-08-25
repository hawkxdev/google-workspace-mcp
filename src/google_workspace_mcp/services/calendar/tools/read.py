"""Register Calendar read tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import CalendarGateway
from ..constants import (
    DEFAULT_PAGE_SIZE,
    MAX_CALENDAR_PAGE_SIZE,
    MAX_EVENT_PAGE_SIZE,
    MAX_FREEBUSY_CALENDARS,
    MAX_ID_CHARS,
)
from ..schemas import (
    CalendarListResponse,
    EventDetail,
    EventListResponse,
    FreeBusyResponse,
)
from .common import run_gateway

CalendarIdInput = Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS)]


def register_read_tools(
    registrar: ToolRegistrar,
    gateway: CalendarGateway,
) -> None:
    """Register Calendar read tools."""
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='calendar_list_calendars',
        title='List Calendars',
        description='List accessible calendars with cursor pagination.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def calendar_list_calendars(
        page_size: Annotated[int, Field(ge=1, le=MAX_CALENDAR_PAGE_SIZE)] = 50,
        page_token: Annotated[str | None, Field(max_length=2048)] = None,
    ) -> CalendarListResponse:
        """List accessible Calendar entries."""
        return await run_gateway(gateway.list_calendars, page_size, page_token)

    @registrar.tool(
        name='calendar_search_events',
        title='Search Calendar Events',
        description='Search expanded events inside a bounded time window.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def calendar_search_events(
        calendar_id: Annotated[str, Field(min_length=1, max_length=256)],
        time_min: Annotated[str, Field(min_length=1, max_length=128)],
        time_max: Annotated[str, Field(min_length=1, max_length=128)],
        query: Annotated[str, Field(max_length=500)] = '',
        page_size: Annotated[
            int, Field(ge=1, le=MAX_EVENT_PAGE_SIZE)
        ] = DEFAULT_PAGE_SIZE,
        page_token: Annotated[str | None, Field(max_length=2048)] = None,
        time_zone: Annotated[str, Field(min_length=1, max_length=128)] = 'UTC',
    ) -> EventListResponse:
        """Search bounded Calendar events."""
        return await run_gateway(
            gateway.search_events,
            calendar_id,
            time_min,
            time_max,
            query,
            page_size,
            page_token,
            time_zone,
        )

    @registrar.tool(
        name='calendar_get_event',
        title='Get Calendar Event',
        description='Read one normalized calendar event.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def calendar_get_event(
        calendar_id: Annotated[str, Field(min_length=1, max_length=256)],
        event_id: Annotated[str, Field(min_length=1, max_length=256)],
    ) -> EventDetail:
        """Read one Calendar event."""
        return await run_gateway(gateway.get_event, calendar_id, event_id)

    @registrar.tool(
        name='calendar_list_event_instances',
        title='List Event Instances',
        description='List recurring instances in a bounded window.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def calendar_list_event_instances(
        calendar_id: Annotated[str, Field(min_length=1, max_length=256)],
        recurring_event_id: Annotated[
            str, Field(min_length=1, max_length=256)
        ],
        time_min: Annotated[str, Field(min_length=1, max_length=128)],
        time_max: Annotated[str, Field(min_length=1, max_length=128)],
        page_size: Annotated[
            int, Field(ge=1, le=MAX_EVENT_PAGE_SIZE)
        ] = DEFAULT_PAGE_SIZE,
        page_token: Annotated[str | None, Field(max_length=2048)] = None,
        time_zone: Annotated[str, Field(min_length=1, max_length=128)] = 'UTC',
    ) -> EventListResponse:
        """List recurring Calendar instances."""
        return await run_gateway(
            gateway.list_instances,
            calendar_id,
            recurring_event_id,
            time_min,
            time_max,
            page_size,
            page_token,
            time_zone,
        )

    @registrar.tool(
        name='calendar_get_freebusy',
        title='Get Calendar Availability',
        description='Return free busy periods and per-calendar errors.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def calendar_get_freebusy(
        calendar_ids: Annotated[
            list[CalendarIdInput],
            Field(min_length=1, max_length=MAX_FREEBUSY_CALENDARS),
        ],
        time_min: Annotated[str, Field(min_length=1, max_length=128)],
        time_max: Annotated[str, Field(min_length=1, max_length=128)],
        time_zone: Annotated[str, Field(min_length=1, max_length=128)] = 'UTC',
    ) -> FreeBusyResponse:
        """Read Calendar availability data."""
        return await run_gateway(
            gateway.get_freebusy,
            calendar_ids,
            time_min,
            time_max,
            time_zone,
        )
