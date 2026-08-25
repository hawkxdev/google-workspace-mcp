"""Test Gmail MCP tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.gmail.constants import MAX_ATTACHMENT_BYTES
from google_workspace_mcp.services.gmail.schemas import (
    AttachmentPayload,
    AttachmentSummary,
    MessageDetail,
    MessageSummary,
    SearchMessagesResponse,
)
from google_workspace_mcp.services.gmail.tools import register_gmail_tools
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

TOOL_NAMES = {
    'gmail_search_messages',
    'gmail_search_threads',
    'gmail_get_message',
    'gmail_get_thread',
    'gmail_list_labels',
    'gmail_modify_labels',
    'gmail_archive',
    'gmail_mark_read',
    'gmail_mark_unread',
    'gmail_download_attachment',
    'gmail_list_drafts',
    'gmail_get_draft',
    'gmail_create_draft',
    'gmail_update_draft',
    'gmail_delete_draft',
    'gmail_send_draft',
    'gmail_send_message',
    'gmail_reply',
}

READONLY_TOOLS = {
    'gmail_search_messages',
    'gmail_search_threads',
    'gmail_get_message',
    'gmail_get_thread',
    'gmail_list_labels',
    'gmail_list_drafts',
    'gmail_get_draft',
}


class UnusedGateway:
    """Reject unexpected gateway calls."""

    def __getattr__(self, name: str) -> object:
        """Return rejecting gateway operation."""

        def operation(*_: object, **__: object) -> object:
            """Reject unexpected gateway call."""
            raise AssertionError(f'unexpected gateway call: {name}')

        return operation


@pytest.mark.asyncio
async def test_registers_exact_inventory_with_flat_schemas() -> None:
    server = PolicyMCPServer('gmail')
    register_gmail_tools(ToolRegistrar(server), UnusedGateway(), object())
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
        tool for tool in tools if tool.name == 'gmail_search_messages'
    )
    assert set(search.input_schema['properties']) == {
        'query',
        'page_size',
        'page_token',
    }
    assert 'params' not in search.input_schema['properties']
    assert search.output_schema is not None


def test_annotations_match_side_effects() -> None:
    server = PolicyMCPServer('gmail')
    register_gmail_tools(ToolRegistrar(server), UnusedGateway(), object())
    tools = server._tool_manager.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert by_name['gmail_search_messages'].annotations.read_only_hint is True
    assert by_name['gmail_archive'].annotations.idempotent_hint is True
    assert (
        by_name['gmail_download_attachment'].annotations.read_only_hint
        is False
    )
    assert by_name['gmail_delete_draft'].annotations.destructive_hint is True
    assert by_name['gmail_send_message'].annotations.destructive_hint is True
    assert by_name['gmail_reply'].annotations.idempotent_hint is False


@pytest.mark.asyncio
async def test_search_tool_executes_with_structured_output() -> None:
    class SearchGateway(UnusedGateway):
        """Return one search response."""

        def search_messages(
            self, query: str, page_size: int, page_token: str | None
        ) -> SearchMessagesResponse:
            """Return deterministic search response."""
            assert (query, page_size, page_token) == ('from:alice', 1, None)
            return SearchMessagesResponse(
                items=(
                    MessageSummary(
                        message_id='m1',
                        thread_id='t1',
                        subject='Hello',
                    ),
                )
            )

    server = PolicyMCPServer('gmail')
    register_gmail_tools(ToolRegistrar(server), SearchGateway(), object())
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
        result = await server.call_tool(
            'gmail_search_messages',
            {'query': 'from:alice', 'page_size': 1, 'page_token': None},
        )
    finally:
        context.reset_request_context(token)
    assert result.structured_content['items'][0]['subject'] == 'Hello'


@pytest.mark.asyncio
async def test_download_tool_binds_descriptor_size_before_storage() -> None:
    class AttachmentGateway(UnusedGateway):
        """Return mismatched attachment data."""

        def __init__(self, descriptor_size: int, payload_size: int) -> None:
            """Initialize attachment gateway fake."""
            self.descriptor_size = descriptor_size
            self.payload_size = payload_size
            self.fetch_calls = 0

        def get_message(self, message_id: str) -> MessageDetail:
            """Return attachment descriptor."""
            return MessageDetail(
                message_id=message_id,
                thread_id='t1',
                attachments=(
                    AttachmentSummary(
                        attachment_id='a1',
                        filename='file.bin',
                        size=self.descriptor_size,
                    ),
                ),
            )

        def get_attachment(
            self, message_id: str, attachment_id: str
        ) -> AttachmentPayload:
            """Return attachment payload."""
            del message_id, attachment_id
            self.fetch_calls += 1
            return AttachmentPayload(
                attachment_id='a1',
                encoded_data='eA',
                size=self.payload_size,
            )

    class RejectingStore:
        """Reject unexpected attachment storage."""

        @property
        def directory(self) -> Path:
            return Path.cwd()

        def publish_bytes(self, *args: object) -> object:
            """Reject unexpected publish call."""
            raise AssertionError(f'unexpected publish: {args!r}')

    async def invoke(gateway: AttachmentGateway) -> None:
        """Invoke attachment tool once."""
        server = PolicyMCPServer('gmail')
        register_gmail_tools(ToolRegistrar(server), gateway, RejectingStore())
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
            await server.call_tool(
                'gmail_download_attachment',
                {'message_id': 'm1', 'attachment_id': 'a1'},
            )
        finally:
            context.reset_request_context(token)

    oversized = AttachmentGateway(MAX_ATTACHMENT_BYTES + 1, 1)
    with pytest.raises(Exception, match='attachment is too large'):
        await invoke(oversized)
    assert oversized.fetch_calls == 0

    mismatched = AttachmentGateway(2, 1)
    with pytest.raises(Exception, match='attachment size is invalid'):
        await invoke(mismatched)
    assert mismatched.fetch_calls == 1


@pytest.mark.asyncio
async def test_download_tool_decodes_and_publishes_attachment(
    tmp_path: Path,
) -> None:
    class SuccessGateway(UnusedGateway):
        def get_message(self, message_id: str) -> MessageDetail:
            return MessageDetail(
                message_id=message_id,
                thread_id='t1',
                attachments=(
                    AttachmentSummary(
                        attachment_id='a1',
                        filename='report.pdf',
                        size=4,
                    ),
                ),
            )

        def get_attachment(
            self, message_id: str, attachment_id: str
        ) -> AttachmentPayload:
            import base64

            del message_id, attachment_id
            return AttachmentPayload(
                attachment_id='a1',
                encoded_data=base64.urlsafe_b64encode(b'data')
                .decode()
                .rstrip('='),
                size=4,
            )

    from google_workspace_mcp.common.managed_files import ManagedFileStore

    store = ManagedFileStore(tmp_path, max_bytes=100)
    server = PolicyMCPServer('gmail')
    register_gmail_tools(ToolRegistrar(server), SuccessGateway(), store)
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
        result = await server.call_tool(
            'gmail_download_attachment',
            {'message_id': 'm1', 'attachment_id': 'a1'},
        )
    finally:
        context.reset_request_context(token)

    structured = result.structured_content
    assert structured['size'] == 4
    assert structured['filename'].endswith('report.pdf')
    assert (tmp_path / structured['filename']).read_bytes() == b'data'
