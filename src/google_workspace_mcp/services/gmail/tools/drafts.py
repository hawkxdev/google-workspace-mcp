"""Register Gmail draft tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import GmailGateway
from ..constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from ..schemas import (
    DraftDeletion,
    DraftDetail,
    DraftsResponse,
    DraftSummary,
    SentMessage,
)
from .common import Recipient, run_gateway


def register_draft_tools(
    registrar: ToolRegistrar,
    gateway: GmailGateway,
) -> None:
    """Register Gmail draft tools."""
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='gmail_list_drafts',
        title='List Gmail Drafts',
        description='List Gmail drafts with cursor pagination.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_list_drafts(
        page_size: Annotated[
            int, Field(ge=1, le=MAX_PAGE_SIZE)
        ] = DEFAULT_PAGE_SIZE,
        page_token: Annotated[str | None, Field(max_length=2048)] = None,
    ) -> DraftsResponse:
        """List paged Gmail drafts."""
        return await run_gateway(
            gateway.list_drafts,
            page_size,
            page_token,
        )

    @registrar.tool(
        name='gmail_get_draft',
        title='Get Gmail Draft',
        description='Read one Gmail draft.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def gmail_get_draft(
        draft_id: Annotated[str, Field(min_length=1, max_length=256)],
    ) -> DraftDetail:
        """Read one Gmail draft."""
        return await run_gateway(gateway.get_draft, draft_id)

    compose = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @registrar.tool(
        name='gmail_create_draft',
        title='Create Gmail Draft',
        description='Create one plain text Gmail draft.',
        annotations=compose,
        structured_output=True,
    )
    async def gmail_create_draft(
        to: Annotated[list[Recipient], Field(min_length=1, max_length=100)],
        subject: Annotated[str, Field(min_length=1, max_length=998)],
        body: Annotated[str, Field(min_length=1, max_length=100_000)],
        cc: Annotated[list[Recipient], Field(max_length=100)] = [],
        bcc: Annotated[list[Recipient], Field(max_length=100)] = [],
    ) -> DraftSummary:
        """Create plain Gmail draft."""
        return await run_gateway(
            gateway.create_draft,
            to,
            subject,
            body,
            cc,
            bcc,
        )

    @registrar.tool(
        name='gmail_update_draft',
        title='Update Gmail Draft',
        description='Replace one plain text Gmail draft.',
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def gmail_update_draft(
        draft_id: Annotated[str, Field(min_length=1, max_length=256)],
        to: Annotated[list[Recipient], Field(min_length=1, max_length=100)],
        subject: Annotated[str, Field(min_length=1, max_length=998)],
        body: Annotated[str, Field(min_length=1, max_length=100_000)],
        cc: Annotated[list[Recipient], Field(max_length=100)] = [],
        bcc: Annotated[list[Recipient], Field(max_length=100)] = [],
    ) -> DraftSummary:
        """Replace plain Gmail draft."""
        return await run_gateway(
            gateway.update_draft,
            draft_id,
            to,
            subject,
            body,
            cc,
            bcc,
        )

    destructive = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @registrar.tool(
        name='gmail_delete_draft',
        title='Delete Gmail Draft',
        description='Permanently delete one Gmail draft.',
        annotations=destructive,
        structured_output=True,
    )
    async def gmail_delete_draft(
        draft_id: Annotated[str, Field(min_length=1, max_length=256)],
    ) -> DraftDeletion:
        """Delete one Gmail draft."""
        return await run_gateway(gateway.delete_draft, draft_id)

    @registrar.tool(
        name='gmail_send_draft',
        title='Send Gmail Draft',
        description='Send one existing Gmail draft.',
        annotations=destructive,
        structured_output=True,
    )
    async def gmail_send_draft(
        draft_id: Annotated[str, Field(min_length=1, max_length=256)],
    ) -> SentMessage:
        """Send one Gmail draft."""
        return await run_gateway(gateway.send_draft, draft_id)
