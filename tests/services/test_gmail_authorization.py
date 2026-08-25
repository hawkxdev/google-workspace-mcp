"""Test Gmail tool authorization."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.gmail.tools import register_gmail_tools
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

from .test_gmail_tools import READONLY_TOOLS, UnusedGateway


@pytest.mark.asyncio
async def test_readonly_principal_only_sees_read_tools() -> None:
    server = PolicyMCPServer('gmail')
    register_gmail_tools(ToolRegistrar(server), UnusedGateway(), object())
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
                'gmail_send_message',
                {'to': ['alice@example.com'], 'subject': 'x', 'body': 'y'},
            )
    finally:
        context.reset_request_context(token)
