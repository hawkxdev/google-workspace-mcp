"""Test Drive tool authorization."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.drive.tools import register_drive_tools
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

from .test_drive_tools import (
    READONLY_TOOL_NAMES,
    UnusedFileStore,
    UnusedGateway,
)


@pytest.mark.asyncio
async def test_readonly_principal_only_sees_read_tools() -> None:
    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        UnusedGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )
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
        assert {tool.name for tool in tools} == READONLY_TOOL_NAMES

        forbidden_mutations: dict[str, dict[str, Any]] = {
            'drive_download_file': {'file_id': 'file-123'},
            'drive_export_file': {
                'file_id': 'doc-123',
                'export_format': 'pdf',
            },
            'drive_create_folder': {
                'name': 'Projects',
                'parent_id': 'folder-1',
            },
            'drive_upload_file': {
                'managed_name': 'test.txt',
                'expected_size': 12,
                'expected_sha256': 'a' * 64,
                'name': 'test.txt',
                'mime_type': 'text/plain',
                'parent_id': 'folder-1',
            },
            'drive_update_file': {
                'file_id': 'file-123',
                'expected_version': 2,
                'name': 'renamed.txt',
            },
            'drive_move_file': {
                'file_id': 'file-123',
                'expected_version': 2,
                'destination_parent_id': 'folder-456',
            },
            'drive_copy_file': {
                'file_id': 'file-123',
                'name': 'copy.txt',
                'parent_id': 'folder-456',
            },
        }

        for tool_name, arguments in forbidden_mutations.items():
            with pytest.raises(ToolError, match='Forbidden'):
                await server.call_tool(tool_name, arguments)
    finally:
        context.reset_request_context(token)
