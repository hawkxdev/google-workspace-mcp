"""Register Gmail mutation tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..attachments import ManagedAttachmentStore
from ..client import GmailGateway
from ..constants import MAX_ATTACHMENT_BYTES
from ..errors import GmailAttachmentError
from ..schemas import DownloadedAttachment, MutationResult, TargetType
from .common import run_gateway

LabelId = Annotated[
    str,
    Field(min_length=1, max_length=256, description='Gmail label ID'),
]


def _target_annotations() -> ToolAnnotations:
    """Build reversible tool annotations."""
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )


def register_mutation_tools(
    registrar: ToolRegistrar,
    gateway: GmailGateway,
    attachments: ManagedAttachmentStore,
) -> None:
    """Register Gmail mutation tools."""

    @registrar.tool(
        name='gmail_modify_labels',
        title='Modify Gmail Labels',
        description='Add or remove labels from one Gmail message or thread.',
        annotations=_target_annotations(),
        structured_output=True,
    )
    async def gmail_modify_labels(
        target_type: Annotated[
            TargetType, Field(description='Message or thread target')
        ],
        target_id: Annotated[
            str, Field(min_length=1, max_length=256, description='Target ID')
        ],
        add_label_ids: Annotated[
            list[LabelId], Field(max_length=100, description='Labels to add')
        ] = [],
        remove_label_ids: Annotated[
            list[LabelId],
            Field(max_length=100, description='Labels to remove'),
        ] = [],
    ) -> MutationResult:
        """Modify one Gmail target."""
        return await run_gateway(
            gateway.modify_labels,
            target_type,
            target_id,
            add_label_ids,
            remove_label_ids,
        )

    def register_state_tool(
        name: str,
        title: str,
        description: str,
        operation: Callable[[TargetType, str], MutationResult],
    ) -> None:
        """Register Gmail state tool."""

        @registrar.tool(
            name=name,
            title=title,
            description=description,
            annotations=_target_annotations(),
            structured_output=True,
        )
        async def state_tool(
            target_type: TargetType,
            target_id: Annotated[str, Field(min_length=1, max_length=256)],
        ) -> MutationResult:
            """Change one Gmail target."""
            return await run_gateway(operation, target_type, target_id)

    register_state_tool(
        'gmail_archive',
        'Archive Gmail Target',
        'Archive one Gmail message or thread.',
        gateway.archive,
    )
    register_state_tool(
        'gmail_mark_read',
        'Mark Gmail Target Read',
        'Mark one Gmail message or thread as read.',
        gateway.mark_read,
    )
    register_state_tool(
        'gmail_mark_unread',
        'Mark Gmail Target Unread',
        'Mark one Gmail message or thread as unread.',
        gateway.mark_unread,
    )

    @registrar.tool(
        name='gmail_download_attachment',
        title='Download Gmail Attachment',
        description='Download one attachment into managed service storage.',
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def gmail_download_attachment(
        message_id: Annotated[str, Field(min_length=1, max_length=256)],
        attachment_id: Annotated[str, Field(min_length=1, max_length=1024)],
    ) -> DownloadedAttachment:
        """Download one Gmail attachment."""

        def download() -> DownloadedAttachment:
            """Fetch and store attachment."""
            message = gateway.get_message(message_id)
            descriptor = next(
                (
                    item
                    for item in message.attachments
                    if item.attachment_id == attachment_id
                ),
                None,
            )
            if descriptor is None:
                raise GmailAttachmentError('attachment was not found')
            if descriptor.size > MAX_ATTACHMENT_BYTES:
                raise GmailAttachmentError('attachment is too large')
            payload = gateway.get_attachment(message_id, attachment_id)
            if payload.size != descriptor.size:
                raise GmailAttachmentError('attachment size is invalid')
            return attachments.save(
                message_id,
                attachment_id,
                descriptor.filename,
                descriptor.size,
                payload.encoded_data,
            )

        return await run_gateway(download)
