"""Test Sheets tool authorization."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.sheets.tools import register_sheets_tools
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

from .test_sheets_tools import (
    READONLY_SHEETS_TOOLS,
    SHEETS_TOOL_NAMES,
    FakeGateway,
)


@pytest.mark.asyncio
async def test_readonly_principal_only_sees_read_tools() -> None:
    server = PolicyMCPServer('sheets')
    gateway = FakeGateway()
    register_sheets_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    principal = context.AuthenticatedPrincipal(
        principal_id='readonly_principal',
        credential_id='0' * 64,
        client_id='client',
        policy='mcp_readonly_v1',
        capabilities=frozenset(server.readonly_capabilities()),
        full_access=False,
    )
    token = context.set_request_context(principal, 'request')
    try:
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == READONLY_SHEETS_TOOLS

        forbidden_mutations: dict[str, dict[str, Any]] = {
            'sheets_update_range': {
                'spreadsheet_id': 'book-1',
                'range_name': 'Sheet1!A1',
                'values': [[1]],
                'input_mode': 'raw',
            },
            'sheets_append_rows': {
                'spreadsheet_id': 'book-1',
                'range_name': 'Sheet1!A1',
                'values': [[1]],
                'input_mode': 'raw',
                'insert_mode': 'insert_rows',
            },
            'sheets_batch_update_ranges': {
                'spreadsheet_id': 'book-1',
                'data': [{'range_name': 'Sheet1!A1', 'values': [[1]]}],
                'input_mode': 'raw',
            },
            'sheets_clear_ranges': {
                'spreadsheet_id': 'book-1',
                'ranges': ['Sheet1!A1'],
            },
            'sheets_create_spreadsheet': {
                'title': 'Forbidden Spreadsheet',
            },
            'sheets_add_sheet': {
                'spreadsheet_id': 'book-1',
                'title': 'Forbidden Sheet',
            },
            'sheets_rename_sheet': {
                'spreadsheet_id': 'book-1',
                'sheet_id': 0,
                'title': 'Forbidden Title',
            },
            'sheets_copy_sheet': {
                'source_spreadsheet_id': 'book-1',
                'sheet_id': 0,
            },
        }

        assert set(forbidden_mutations.keys()) == (
            SHEETS_TOOL_NAMES - READONLY_SHEETS_TOOLS
        )

        for tool_name, arguments in forbidden_mutations.items():
            calls_before = len(gateway.calls)
            with pytest.raises(ToolError) as exc_info:
                await server.call_tool(tool_name, arguments)
            assert (
                'Forbidden: tool is not permitted for this principal'
                in str(exc_info.value)
            )
            assert len(gateway.calls) == calls_before
    finally:
        context.reset_request_context(token)
