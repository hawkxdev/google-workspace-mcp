"""Test Calendar tool authorization."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.calendar.tools import (
    register_calendar_tools,
)
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

from .test_calendar_tools import READONLY_TOOLS, UnusedDependency


@pytest.mark.asyncio
async def test_readonly_principal_only_sees_calendar_reads() -> None:
    server = PolicyMCPServer('calendar')
    dependency = UnusedDependency()
    register_calendar_tools(
        ToolRegistrar(server), dependency, dependency, dependency
    )
    principal = context.AuthenticatedPrincipal(
        principal_id='readonly',
        credential_id='0' * 64,
        client_id='client',
        policy='mcp_readonly_v1',
        capabilities=frozenset(server.readonly_capabilities()),
        full_access=False,
    )
    token = context.set_request_context(principal, 'request')
    try:
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == READONLY_TOOLS
        with pytest.raises(ToolError, match='Forbidden'):
            await server.call_tool(
                'calendar_delete_event',
                {
                    'calendar_id': 'primary',
                    'event_id': 'event-1',
                    'etag': 'etag-1',
                },
            )
    finally:
        context.reset_request_context(token)
