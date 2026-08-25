"""Register Gmail read tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import GmailGateway
from ..constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from ..schemas import (
    LabelsResponse,
    MessageDetail,
    SearchMessagesResponse,
    SearchThreadsResponse,
    ThreadDetail,
)
from .common import run_gateway


def register_read_tools(
    registrar: ToolRegistrar,
    gateway: GmailGateway,
) -> None:
    """Register Gmail read tools."""
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='gmail_search_messages',
        title='Search Gmail Messages',
        description='Search Gmail messages and return bounded summaries.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_search_messages(
        query: Annotated[str, Field(max_length=500)] = '',
        page_size: Annotated[
            int, Field(ge=1, le=MAX_PAGE_SIZE)
        ] = DEFAULT_PAGE_SIZE,
        page_token: Annotated[str | None, Field(max_length=2048)] = None,
    ) -> SearchMessagesResponse:
        """Search bounded Gmail messages."""
        return await run_gateway(
            gateway.search_messages,
            query,
            page_size,
            page_token,
        )

    @registrar.tool(
        name='gmail_search_threads',
        title='Search Gmail Threads',
        description='Search Gmail threads and return bounded summaries.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_search_threads(
        query: Annotated[str, Field(max_length=500)] = '',
        page_size: Annotated[
            int, Field(ge=1, le=MAX_PAGE_SIZE)
        ] = DEFAULT_PAGE_SIZE,
        page_token: Annotated[str | None, Field(max_length=2048)] = None,
    ) -> SearchThreadsResponse:
        """Search bounded Gmail threads."""
        return await run_gateway(
            gateway.search_threads,
            query,
            page_size,
            page_token,
        )

    @registrar.tool(
        name='gmail_get_message',
        title='Get Gmail Message',
        description='Read one Gmail message with bounded normalized content.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_get_message(
        message_id: Annotated[str, Field(min_length=1, max_length=256)],
    ) -> MessageDetail:
        """Read one Gmail message."""
        return await run_gateway(gateway.get_message, message_id)

    @registrar.tool(
        name='gmail_get_thread',
        title='Get Gmail Thread',
        description='Read one Gmail thread with bounded normalized messages.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_get_thread(
        thread_id: Annotated[str, Field(min_length=1, max_length=256)],
    ) -> ThreadDetail:
        """Read one Gmail thread."""
        return await run_gateway(gateway.get_thread, thread_id)

    @registrar.tool(
        name='gmail_list_labels',
        title='List Gmail Labels',
        description='List available Gmail labels.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_list_labels() -> LabelsResponse:
        """List available Gmail labels."""
        return await run_gateway(gateway.list_labels)
