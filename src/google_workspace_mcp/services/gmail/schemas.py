"""Define Gmail service schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GmailModel(BaseModel):
    """Configure Gmail schema model."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class TargetType(StrEnum):
    """Select Gmail target type."""

    MESSAGE = 'message'
    THREAD = 'thread'


class AttachmentSummary(GmailModel):
    """Describe Gmail message attachment."""

    attachment_id: str
    filename: str
    mime_type: str = ''
    size: int = Field(default=0, ge=0)


class ParsedMessage(GmailModel):
    """Store normalized MIME content."""

    subject: str = ''
    sender: str = ''
    recipients: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    date: str = ''
    message_header_id: str = ''
    references: str = ''
    body_text: str = ''
    attachments: tuple[AttachmentSummary, ...] = ()


class MessageSummary(GmailModel):
    """Summarize Gmail message metadata."""

    message_id: str
    thread_id: str
    subject: str = ''
    sender: str = ''
    date: str = ''
    snippet: str = ''
    label_ids: tuple[str, ...] = ()


class MessageDetail(MessageSummary):
    """Describe normalized Gmail message."""

    recipients: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    message_header_id: str = ''
    references: str = ''
    body_text: str = ''
    attachments: tuple[AttachmentSummary, ...] = ()


class ThreadSummary(GmailModel):
    """Summarize Gmail thread metadata."""

    thread_id: str
    subject: str = ''
    sender: str = ''
    date: str = ''
    snippet: str = ''
    label_ids: tuple[str, ...] = ()
    message_count: int = Field(default=0, ge=0)


class ThreadDetail(GmailModel):
    """Describe normalized Gmail thread."""

    thread_id: str
    history_id: str | None = None
    messages: tuple[MessageDetail, ...] = ()


class LabelSummary(GmailModel):
    """Describe Gmail label metadata."""

    label_id: str
    name: str
    label_type: str = ''
    message_list_visibility: str | None = None
    label_list_visibility: str | None = None


class SearchMessagesResponse(GmailModel):
    """Return paged Gmail messages."""

    items: tuple[MessageSummary, ...] = ()
    next_page_token: str | None = None
    result_size_estimate: int | None = Field(default=None, ge=0)


class SearchThreadsResponse(GmailModel):
    """Return paged Gmail threads."""

    items: tuple[ThreadSummary, ...] = ()
    next_page_token: str | None = None
    result_size_estimate: int | None = Field(default=None, ge=0)


class LabelsResponse(GmailModel):
    """Return Gmail label list."""

    items: tuple[LabelSummary, ...] = ()


class MutationResult(GmailModel):
    """Describe Gmail state mutation."""

    target_type: TargetType
    target_id: str
    label_ids: tuple[str, ...] = ()


class AttachmentPayload(GmailModel):
    """Carry fetched attachment payload."""

    attachment_id: str
    encoded_data: str
    size: int = Field(ge=0)


class DownloadedAttachment(GmailModel):
    """Describe managed attachment file."""

    path: str
    filename: str
    size: int = Field(ge=0)


class DraftSummary(GmailModel):
    """Summarize Gmail draft metadata."""

    draft_id: str
    message_id: str = ''
    thread_id: str = ''


class DraftDetail(GmailModel):
    """Describe Gmail draft message."""

    draft_id: str
    message: MessageDetail


class DraftDeletion(GmailModel):
    """Describe deleted Gmail draft."""

    draft_id: str
    deleted: bool = True


class DraftsResponse(GmailModel):
    """Return paged Gmail drafts."""

    items: tuple[DraftSummary, ...] = ()
    next_page_token: str | None = None
    result_size_estimate: int | None = Field(default=None, ge=0)


class SentMessage(GmailModel):
    """Describe sent Gmail message."""

    message_id: str
    thread_id: str
    label_ids: tuple[str, ...] = ()
