"""Test Docs MCP tools."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.docs.constants import (
    MAX_DOCS_BATCH_OPERATIONS,
)
from google_workspace_mcp.services.docs.errors import (
    DocsConflictError,
    DocsInputError,
    DocsNotFoundError,
    DocsProviderError,
    DocsRateLimitError,
    DocsScopeError,
    DocsUnsupportedError,
)
from google_workspace_mcp.services.docs.schemas import (
    DocsBatchOperationType,
    DocsBatchReply,
    DocsBatchResult,
    DocsContentResponse,
    DocsCreateResult,
    DocsMutationResult,
    DocsParagraphBlock,
    DocsReplaceResult,
    DocsTabSummary,
    DocsTextElement,
    DocumentSummary,
)
from google_workspace_mcp.services.docs.schemas import (
    DocsElementKind as ElementKind,
)
from google_workspace_mcp.services.docs.tools import register_docs_tools
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

DOCS_TOOL_NAMES = {
    'docs_get_document',
    'docs_read_content',
    'docs_create_document',
    'docs_insert_text',
    'docs_replace_text',
    'docs_delete_range',
    'docs_batch_update',
}

READONLY_DOCS_TOOLS = {
    'docs_get_document',
    'docs_read_content',
}

DESTRUCTIVE_DOCS_TOOLS = {
    'docs_insert_text',
    'docs_replace_text',
    'docs_delete_range',
    'docs_batch_update',
}


class FakeGateway:
    """Record Docs gateway calls."""

    def __init__(self) -> None:
        """Initialize fake gateway tracker."""
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.errors: dict[str, Exception] = {}

    def _record(self, name: str, args: tuple[Any, ...], kwargs: Any) -> None:
        """Record one gateway call."""
        self.calls.append((name, args, dict(kwargs)))
        error = self.errors.get(name)
        if error is not None:
            raise error

    def get_document(self, document_id: str) -> DocumentSummary:
        """Record get_document call."""
        self._record('get_document', (document_id,), {})
        return DocumentSummary(
            document_id=document_id,
            title='Test Document',
            revision_id='revision-1',
            tabs=(
                DocsTabSummary(
                    tab_id='tab-1',
                    title='Main',
                    index=0,
                    start_index=0,
                    end_index=13,
                ),
            ),
        )

    def read_content(
        self, document_id: str, tab_id: str, **kwargs: Any
    ) -> DocsContentResponse:
        """Record read_content call."""
        self._record('read_content', (document_id, tab_id), kwargs)
        return DocsContentResponse(
            document_id=document_id,
            revision_id='revision-1',
            tab_id=tab_id,
            start_index=1,
            end_index=7,
            blocks=(
                DocsParagraphBlock(
                    start_index=1,
                    end_index=7,
                    named_style='NORMAL_TEXT',
                    elements=(
                        DocsTextElement(
                            kind=ElementKind.TEXT_RUN,
                            start_index=1,
                            end_index=7,
                            content='Hello\n',
                        ),
                    ),
                ),
            ),
            text_characters=6,
        )

    def create_document(self, title: str) -> DocsCreateResult:
        """Record create_document call."""
        self._record('create_document', (title,), {})
        return DocsCreateResult(
            document_id='document-9',
            title=title,
            tab_id='tab-1',
            required_revision_id='revision-1',
        )

    def insert_text(
        self,
        document_id: str,
        tab_id: str,
        index: int,
        text: str,
        **kwargs: Any,
    ) -> DocsMutationResult:
        """Record insert_text call."""
        self._record('insert_text', (document_id, tab_id, index, text), kwargs)
        return DocsMutationResult(
            document_id=document_id,
            tab_id=tab_id,
            required_revision_id='revision-2',
        )

    def delete_range(
        self,
        document_id: str,
        tab_id: str,
        start_index: int,
        end_index: int,
        **kwargs: Any,
    ) -> DocsMutationResult:
        """Record delete_range call."""
        self._record(
            'delete_range',
            (document_id, tab_id, start_index, end_index),
            kwargs,
        )
        return DocsMutationResult(
            document_id=document_id,
            tab_id=tab_id,
            required_revision_id='revision-2',
        )

    def replace_text(
        self,
        document_id: str,
        tab_id: str,
        search_text: str,
        replacement_text: str,
        **kwargs: Any,
    ) -> DocsReplaceResult:
        """Record replace_text call."""
        self._record(
            'replace_text',
            (document_id, tab_id, search_text, replacement_text),
            kwargs,
        )
        return DocsReplaceResult(
            document_id=document_id,
            tab_id=tab_id,
            occurrences_changed=1,
            required_revision_id='revision-2',
        )

    def batch_update(
        self,
        document_id: str,
        tab_id: str,
        operations: Any,
        **kwargs: Any,
    ) -> DocsBatchResult:
        """Record batch_update call."""
        self._record('batch_update', (document_id, tab_id, operations), kwargs)
        return DocsBatchResult(
            document_id=document_id,
            tab_id=tab_id,
            operation_count=len(list(operations)),
            required_revision_id='revision-2',
            replies=(
                DocsBatchReply(
                    operation=DocsBatchOperationType.INSERT_TEXT,
                ),
            ),
        )


def build_server(gateway: FakeGateway) -> PolicyMCPServer:
    """Build server with tools."""
    server = PolicyMCPServer('docs')
    register_docs_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]
    return server


def set_full_principal() -> Any:
    """Set full access principal."""
    principal = context.AuthenticatedPrincipal(
        principal_id='full_principal',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    return context.set_request_context(principal, 'request')


@pytest.mark.asyncio
async def test_registers_exact_docs_inventory() -> None:
    server = build_server(FakeGateway())
    token = set_full_principal()
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)
    assert {tool.name for tool in tools} == DOCS_TOOL_NAMES
    assert set(server.readonly_capabilities()) == READONLY_DOCS_TOOLS


@pytest.mark.asyncio
async def test_docs_annotations_match_design() -> None:
    server = build_server(FakeGateway())
    token = set_full_principal()
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)
    annotations = {tool.name: tool.annotations for tool in tools}
    for name, annotation in annotations.items():
        assert annotation is not None
        assert annotation.open_world_hint is True
        assert annotation.read_only_hint is (name in READONLY_DOCS_TOOLS)
        assert annotation.destructive_hint is (name in DESTRUCTIVE_DOCS_TOOLS)
        assert annotation.idempotent_hint is (name in READONLY_DOCS_TOOLS)


@pytest.mark.asyncio
async def test_every_tool_declares_structured_output() -> None:
    server = build_server(FakeGateway())
    token = set_full_principal()
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)
    for tool in tools:
        assert tool.output_schema is not None


@pytest.mark.asyncio
async def test_mutations_require_tab_and_revision() -> None:
    server = build_server(FakeGateway())
    token = set_full_principal()
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)
    schemas = {tool.name: tool.input_schema for tool in tools}
    for name in DESTRUCTIVE_DOCS_TOOLS:
        required = set(schemas[name].get('required', ()))
        assert 'tab_id' in required
        assert 'required_revision_id' in required
    create_required = set(schemas['docs_create_document'].get('required', ()))
    assert create_required == {'title'}


@pytest.mark.asyncio
async def test_batch_operations_are_bounded_in_schema() -> None:
    server = build_server(FakeGateway())
    token = set_full_principal()
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)
    schema = next(
        tool.input_schema for tool in tools if tool.name == 'docs_batch_update'
    )
    operations = schema['properties']['operations']
    assert operations['maxItems'] == MAX_DOCS_BATCH_OPERATIONS
    assert operations['minItems'] == 1


@pytest.mark.asyncio
async def test_read_tools_forward_to_gateway() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        summary = await server.call_tool(
            'docs_get_document', {'document_id': 'document-1'}
        )
        content = await server.call_tool(
            'docs_read_content',
            {
                'document_id': 'document-1',
                'tab_id': 'tab-1',
                'max_blocks': 10,
            },
        )
    finally:
        context.reset_request_context(token)
    assert summary.structured_content['revision_id'] == 'revision-1'
    assert content.structured_content['tab_id'] == 'tab-1'
    assert gateway.calls[0][0] == 'get_document'
    assert gateway.calls[1][0] == 'read_content'
    assert gateway.calls[1][2]['max_blocks'] == 10


@pytest.mark.asyncio
async def test_create_tool_forwards_title_only() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        result = await server.call_tool(
            'docs_create_document', {'title': 'New Document'}
        )
    finally:
        context.reset_request_context(token)
    assert result.structured_content['tab_id'] == 'tab-1'
    assert gateway.calls == [('create_document', ('New Document',), {})]


@pytest.mark.asyncio
async def test_insert_tool_forwards_revision() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        result = await server.call_tool(
            'docs_insert_text',
            {
                'document_id': 'document-1',
                'tab_id': 'tab-1',
                'index': 1,
                'text': 'Hello',
                'required_revision_id': 'revision-1',
            },
        )
    finally:
        context.reset_request_context(token)
    assert result.structured_content['required_revision_id'] == 'revision-2'
    name, args, kwargs = gateway.calls[0]
    assert name == 'insert_text'
    assert args == ('document-1', 'tab-1', 1, 'Hello')
    assert kwargs == {'required_revision_id': 'revision-1'}


@pytest.mark.asyncio
async def test_delete_tool_forwards_range() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        await server.call_tool(
            'docs_delete_range',
            {
                'document_id': 'document-1',
                'tab_id': 'tab-1',
                'start_index': 1,
                'end_index': 5,
                'required_revision_id': 'revision-1',
            },
        )
    finally:
        context.reset_request_context(token)
    name, args, kwargs = gateway.calls[0]
    assert name == 'delete_range'
    assert args == ('document-1', 'tab-1', 1, 5)
    assert kwargs == {'required_revision_id': 'revision-1'}


@pytest.mark.asyncio
async def test_replace_tool_forwards_explicit_contract() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        result = await server.call_tool(
            'docs_replace_text',
            {
                'document_id': 'document-1',
                'tab_id': 'tab-1',
                'search_text': 'Hello',
                'replacement_text': 'Goodbye',
                'required_revision_id': 'revision-1',
                'match_case': True,
                'expected_occurrences': 1,
            },
        )
    finally:
        context.reset_request_context(token)
    assert result.structured_content['occurrences_changed'] == 1
    name, args, kwargs = gateway.calls[0]
    assert name == 'replace_text'
    assert args == ('document-1', 'tab-1', 'Hello', 'Goodbye')
    assert kwargs == {
        'required_revision_id': 'revision-1',
        'match_case': True,
        'expected_occurrences': 1,
    }


@pytest.mark.asyncio
async def test_batch_tool_forwards_typed_operations() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        result = await server.call_tool(
            'docs_batch_update',
            {
                'document_id': 'document-1',
                'tab_id': 'tab-1',
                'required_revision_id': 'revision-1',
                'operations': [
                    {
                        'operation': 'insert_text',
                        'index': 1,
                        'text': 'Hello',
                    }
                ],
            },
        )
    finally:
        context.reset_request_context(token)
    assert result.structured_content['operation_count'] == 1
    name, args, kwargs = gateway.calls[0]
    assert name == 'batch_update'
    operations = args[2]
    assert len(operations) == 1
    assert operations[0].operation is DocsBatchOperationType.INSERT_TEXT
    assert kwargs == {'required_revision_id': 'revision-1'}


@pytest.mark.asyncio
async def test_batch_tool_rejects_raw_request_by_schema() -> None:
    gateway = FakeGateway()
    server = build_server(gateway)
    token = set_full_principal()
    try:
        with pytest.raises(Exception) as caught:
            await server.call_tool(
                'docs_batch_update',
                {
                    'document_id': 'document-1',
                    'tab_id': 'tab-1',
                    'required_revision_id': 'revision-1',
                    'operations': [{'insertText': {'text': 'Hello'}}],
                },
            )
    finally:
        context.reset_request_context(token)
    assert gateway.calls == []
    assert caught.value is not None


@pytest.mark.parametrize(
    'error',
    [
        DocsInputError('Docs range is invalid'),
        DocsNotFoundError('Docs resource was not found'),
        DocsConflictError('Docs document revision changed'),
        DocsScopeError('Google authorization lacks required permissions'),
        DocsRateLimitError('Docs is temporarily rate limited'),
        DocsProviderError('Docs request is temporarily unavailable'),
        DocsUnsupportedError('Docs structure is unsupported'),
    ],
)
@pytest.mark.asyncio
async def test_gateway_errors_become_tool_errors(error: Exception) -> None:
    gateway = FakeGateway()
    gateway.errors['get_document'] = error
    server = build_server(gateway)
    token = set_full_principal()
    try:
        with pytest.raises(ToolError) as caught:
            await server.call_tool(
                'docs_get_document', {'document_id': 'document-1'}
            )
    finally:
        context.reset_request_context(token)
    assert str(error) in str(caught.value)


@pytest.mark.asyncio
async def test_tool_errors_do_not_leak_internal_paths() -> None:
    gateway = FakeGateway()
    gateway.errors['get_document'] = DocsProviderError(
        'Docs request is temporarily unavailable'
    )
    server = build_server(gateway)
    token = set_full_principal()
    try:
        with pytest.raises(ToolError) as caught:
            await server.call_tool(
                'docs_get_document', {'document_id': 'document-1'}
            )
    finally:
        context.reset_request_context(token)
    message = str(caught.value)
    assert 'googleapis.com' not in message
    assert '/Users/' not in message
