"""Register Gmail sending tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import GmailGateway
from ..schemas import SentMessage
from .common import Recipient, run_gateway


def register_send_tools(
    registrar: ToolRegistrar,
    gateway: GmailGateway,
) -> None:
    """Register Gmail sending tools."""
    external = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @registrar.tool(
        name='gmail_send_message',
        title='Send Gmail Message',
        description='Send one plain text Gmail message.',
        annotations=external,
        structured_output=True,
    )
    async def gmail_send_message(
        to: Annotated[
            list[Recipient],
            Field(
                min_length=1, max_length=100, description='Primary recipients'
            ),
        ],
        subject: Annotated[
            str, Field(min_length=1, max_length=998, description='Subject')
        ],
        body: Annotated[
            str,
            Field(
                min_length=1, max_length=100_000, description='Plain text body'
            ),
        ],
        cc: Annotated[
            list[Recipient],
            Field(max_length=100, description='Copy recipients'),
        ] = [],
        bcc: Annotated[
            list[Recipient],
            Field(max_length=100, description='Blind copy recipients'),
        ] = [],
    ) -> SentMessage:
        """Send plain Gmail message."""
        return await run_gateway(
            gateway.send_message,
            to,
            subject,
            body,
            cc,
            bcc,
        )

    @registrar.tool(
        name='gmail_reply',
        title='Reply To Gmail Author',
        description='Reply to the author of one Gmail message.',
        annotations=external,
        structured_output=True,
    )
    async def gmail_reply(
        message_id: Annotated[
            str,
            Field(
                min_length=1, max_length=256, description='Source message ID'
            ),
        ],
        body: Annotated[
            str,
            Field(
                min_length=1, max_length=100_000, description='Plain text body'
            ),
        ],
    ) -> SentMessage:
        """Reply to Gmail author."""
        return await run_gateway(
            gateway.reply_to_author,
            message_id,
            body,
        )
