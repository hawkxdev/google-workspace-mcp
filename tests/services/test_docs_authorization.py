"""Test Docs tool authorization."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.docs.tools import register_docs_tools
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

from .test_docs_tools import (
    DOCS_TOOL_NAMES,
    READONLY_DOCS_TOOLS,
    FakeGateway,
)

FORBIDDEN_MUTATIONS: dict[str, dict[str, Any]] = {
    'docs_create_document': {'title': 'Forbidden Document'},
    'docs_insert_text': {
        'document_id': 'document-1',
        'tab_id': 'tab-1',
        'index': 1,
        'text': 'Forbidden',
        'required_revision_id': 'revision-1',
    },
    'docs_replace_text': {
        'document_id': 'document-1',
        'tab_id': 'tab-1',
        'search_text': 'Hello',
        'replacement_text': 'Forbidden',
        'required_revision_id': 'revision-1',
        'match_case': True,
        'expected_occurrences': 1,
    },
    'docs_delete_range': {
        'document_id': 'document-1',
        'tab_id': 'tab-1',
        'start_index': 1,
        'end_index': 5,
        'required_revision_id': 'revision-1',
    },
    'docs_batch_update': {
        'document_id': 'document-1',
        'tab_id': 'tab-1',
        'required_revision_id': 'revision-1',
        'operations': [
            {'operation': 'insert_text', 'index': 1, 'text': 'Forbidden'}
        ],
    },
}


def readonly_principal(server: PolicyMCPServer) -> Any:
    """Bind readonly Docs principal."""
    principal = context.AuthenticatedPrincipal(
        principal_id='readonly_principal',
        credential_id='0' * 64,
        client_id='client',
        policy='mcp_readonly_v1',
        capabilities=frozenset(server.readonly_capabilities()),
        full_access=False,
    )
    return context.set_request_context(principal, 'request')


@pytest.mark.asyncio
async def test_readonly_principal_only_sees_read_tools() -> None:
    server = PolicyMCPServer('docs')
    gateway = FakeGateway()
    register_docs_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    token = readonly_principal(server)
    try:
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == READONLY_DOCS_TOOLS
    finally:
        context.reset_request_context(token)


@pytest.mark.asyncio
async def test_every_mutation_is_forbidden_before_gateway() -> None:
    server = PolicyMCPServer('docs')
    gateway = FakeGateway()
    register_docs_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    assert set(FORBIDDEN_MUTATIONS) == DOCS_TOOL_NAMES - READONLY_DOCS_TOOLS

    token = readonly_principal(server)
    try:
        for tool_name, arguments in FORBIDDEN_MUTATIONS.items():
            calls_before = len(gateway.calls)
            with pytest.raises(ToolError) as caught:
                await server.call_tool(tool_name, arguments)
            assert 'Forbidden: tool is not permitted' in str(caught.value)
            assert len(gateway.calls) == calls_before
    finally:
        context.reset_request_context(token)


@pytest.mark.asyncio
async def test_readonly_principal_may_call_read_tools() -> None:
    server = PolicyMCPServer('docs')
    gateway = FakeGateway()
    register_docs_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    token = readonly_principal(server)
    try:
        await server.call_tool(
            'docs_get_document', {'document_id': 'document-1'}
        )
        await server.call_tool(
            'docs_read_content',
            {'document_id': 'document-1', 'tab_id': 'tab-1'},
        )
    finally:
        context.reset_request_context(token)
    assert [call[0] for call in gateway.calls] == [
        'get_document',
        'read_content',
    ]


@pytest.mark.asyncio
async def test_missing_principal_forbids_every_tool() -> None:
    server = PolicyMCPServer('docs')
    gateway = FakeGateway()
    register_docs_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    tools = await server.list_tools()
    assert tools == []
    for tool_name in DOCS_TOOL_NAMES:
        with pytest.raises(ToolError):
            await server.call_tool(tool_name, {})
    assert gateway.calls == []
