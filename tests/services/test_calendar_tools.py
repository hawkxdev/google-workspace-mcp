"""Test Calendar MCP tools."""

from __future__ import annotations

import pytest

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.calendar.tools import (
    register_calendar_tools,
)
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

TOOL_NAMES = {
    'calendar_list_calendars',
    'calendar_search_events',
    'calendar_get_event',
    'calendar_list_event_instances',
    'calendar_get_freebusy',
    'calendar_create_event',
    'calendar_update_event',
    'calendar_delete_event',
    'calendar_batch_mutate_events',
}
READONLY_TOOLS = {
    'calendar_list_calendars',
    'calendar_search_events',
    'calendar_get_event',
    'calendar_list_event_instances',
    'calendar_get_freebusy',
}


class UnusedDependency:
    """Reject unexpected dependency calls."""

    def __getattr__(self, name: str) -> object:
        """Return rejecting dependency operation."""

        def operation(*_: object, **__: object) -> object:
            """Reject unexpected dependency call."""
            raise AssertionError(f'unexpected dependency call: {name}')

        return operation


@pytest.mark.asyncio
async def test_registers_exact_calendar_inventory() -> None:
    server = PolicyMCPServer('calendar')
    dependency = UnusedDependency()
    register_calendar_tools(
        ToolRegistrar(server), dependency, dependency, dependency
    )
    principal = context.AuthenticatedPrincipal(
        principal_id='full',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)
    assert {tool.name for tool in tools} == TOOL_NAMES
    assert set(server.readonly_capabilities()) == READONLY_TOOLS
    search = next(
        tool for tool in tools if tool.name == 'calendar_search_events'
    )
    assert 'params' not in search.input_schema['properties']
    assert set(search.input_schema['properties']) == {
        'calendar_id',
        'time_min',
        'time_max',
        'query',
        'page_size',
        'page_token',
        'time_zone',
    }
    assert search.output_schema is not None
    public = {tool.name: tool for tool in tools}
    freebusy = public['calendar_get_freebusy'].input_schema['properties']
    assert freebusy['calendar_ids']['items']['maxLength'] == 256
    create = public['calendar_create_event'].input_schema['properties']
    assert create['attendees']['anyOf'][0]['items']['maxLength'] == 320
    assert create['recurrence']['anyOf'][0]['items']['maxLength'] == 1_000
    batch_schema = public['calendar_batch_mutate_events'].input_schema
    assert (
        batch_schema['$defs']['BatchEventBody']['additionalProperties']
        is False
    )
    assert (
        batch_schema['$defs']['BatchOperation']['properties']['calendar_id'][
            'maxLength'
        ]
        == 256
    )


def test_calendar_annotations_match_side_effects() -> None:
    server = PolicyMCPServer('calendar')
    dependency = UnusedDependency()
    register_calendar_tools(
        ToolRegistrar(server), dependency, dependency, dependency
    )
    by_name = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert by_name['calendar_search_events'].annotations.read_only_hint is True
    assert (
        by_name['calendar_create_event'].annotations.destructive_hint is False
    )
    assert (
        by_name['calendar_update_event'].annotations.destructive_hint is True
    )
    assert (
        by_name['calendar_update_event'].annotations.idempotent_hint is False
    )
    assert (
        by_name['calendar_delete_event'].annotations.destructive_hint is True
    )
    assert (
        by_name['calendar_batch_mutate_events'].annotations.destructive_hint
        is True
    )
