"""Call Gmail provider methods."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)

from .constants import (
    MAX_HEADER_CHARS,
    MAX_LABEL_IDS,
    MAX_LABELS,
    MAX_PAGE_SIZE,
    MAX_SNIPPET_CHARS,
    MAX_THREAD_MESSAGES,
    MAX_THREAD_PROVIDER_MESSAGES,
    METADATA_HEADERS,
    REQUEST_RETRIES,
    USER_ID,
)
from .errors import GmailInputError, GmailProviderError
from .mime import (
    build_plain_message,
    build_reply_message,
    find_inline_attachment,
    parse_message,
)
from .schemas import (
    AttachmentPayload,
    DraftDeletion,
    DraftDetail,
    DraftsResponse,
    DraftSummary,
    LabelsResponse,
    LabelSummary,
    MessageDetail,
    MessageSummary,
    MutationResult,
    SearchMessagesResponse,
    SearchThreadsResponse,
    SentMessage,
    TargetType,
    ThreadDetail,
    ThreadSummary,
)

ServiceBuilder = Callable[[GoogleCredentials], Any]


def build_gmail_service(credentials: GoogleCredentials) -> Any:
    """Build Gmail provider service."""
    return build(
        'gmail',
        'v1',
        credentials=credentials.to_google_credentials(),
        cache_discovery=False,
        static_discovery=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    """Require provider response mapping."""
    if not isinstance(value, Mapping):
        raise GmailProviderError('Gmail returned an invalid response')
    return value


def _string_tuple(
    value: Any,
    *,
    max_items: int = MAX_LABEL_IDS,
    max_chars: int = MAX_HEADER_CHARS,
) -> tuple[str, ...]:
    """Normalize provider string list."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(str(item)[:max_chars] for item in value[:max_items])


def _bounded_text(value: Any, limit: int = MAX_HEADER_CHARS) -> str:
    """Normalize bounded provider text."""
    return str(value or '')[:limit]


def _thread_label_ids(value: Any) -> tuple[str, ...]:
    """Normalize Gmail thread labels."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise GmailProviderError('Gmail returned an invalid response')
    if len(value) > MAX_THREAD_PROVIDER_MESSAGES:
        raise GmailProviderError('Gmail returned an invalid response')
    label_order: dict[str, None] = {}
    for message in value:
        message_data = _mapping(message)
        for label in _string_tuple(message_data.get('labelIds')):
            label_order.setdefault(label, None)
            if len(label_order) == MAX_LABEL_IDS:
                break
        if len(label_order) == MAX_LABEL_IDS:
            break
    return tuple(label_order)


class GmailGateway:
    """Normalize Gmail provider operations."""

    def __init__(
        self,
        store: GoogleCredentialStore,
        *,
        service_builder: ServiceBuilder = build_gmail_service,
        num_retries: int = REQUEST_RETRIES,
    ) -> None:
        """Initialize Gmail provider gateway."""
        self._store = store
        self._service_builder = service_builder
        self._num_retries = num_retries

    def _service(self) -> Any:
        """Build authenticated Gmail service."""
        try:
            credentials = self._store.refresh()
            return self._service_builder(credentials)
        except GmailProviderError:
            raise
        except Exception:
            raise GmailProviderError(
                'Gmail credentials are unavailable'
            ) from None

    @staticmethod
    def _http_reason(error: HttpError) -> str | None:
        """Read safe provider reason."""
        try:
            content = json.loads(error.content.decode('utf-8'))
            errors = content.get('error', {}).get('errors', [])
            if isinstance(errors, list) and errors:
                reason = errors[0].get('reason')
                return reason if isinstance(reason, str) else None
        except AttributeError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        return None

    def _execute_raw(self, request: Any) -> Any:
        """Execute raw Gmail request."""
        try:
            return request.execute(num_retries=self._num_retries)
        except HttpError as error:
            status = int(getattr(error.resp, 'status', 0))
            reason = self._http_reason(error)
            if status in {403, 429} and reason in {
                'rateLimitExceeded',
                'userRateLimitExceeded',
            }:
                message = 'Gmail is temporarily rate limited'
            else:
                messages = {
                    400: 'Gmail rejected the request',
                    401: 'Google authorization requires renewal',
                    403: 'Gmail request was forbidden',
                    404: 'Gmail resource was not found',
                    429: 'Gmail is temporarily rate limited',
                }
                message = messages.get(
                    status, 'Gmail request is temporarily unavailable'
                )
            raise GmailProviderError(message) from None
        except TransportError, TimeoutError, ConnectionError, OSError:
            raise GmailProviderError(
                'Gmail request is temporarily unavailable'
            ) from None

    def _execute(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped Gmail request."""
        return _mapping(self._execute_raw(request))

    def _execute_empty(self, request: Any) -> None:
        """Execute empty Gmail request."""
        value = self._execute_raw(request)
        if value not in (None, '', {}):
            raise GmailProviderError('Gmail returned an invalid response')

    @staticmethod
    def _message_summary(data: Mapping[str, Any]) -> MessageSummary:
        """Normalize Gmail message summary."""
        payload_value = data.get('payload')
        payload = payload_value if isinstance(payload_value, Mapping) else {}
        parsed = parse_message(payload)
        return MessageSummary(
            message_id=_bounded_text(data.get('id'), 256),
            thread_id=_bounded_text(data.get('threadId'), 256),
            subject=parsed.subject,
            sender=parsed.sender,
            date=parsed.date,
            snippet=_bounded_text(data.get('snippet'), MAX_SNIPPET_CHARS),
            label_ids=_string_tuple(data.get('labelIds')),
        )

    @staticmethod
    def _message_detail(data: Mapping[str, Any]) -> MessageDetail:
        """Normalize Gmail message detail."""
        payload_value = data.get('payload')
        payload = payload_value if isinstance(payload_value, Mapping) else {}
        parsed = parse_message(payload)
        return MessageDetail(
            message_id=_bounded_text(data.get('id'), 256),
            thread_id=_bounded_text(data.get('threadId'), 256),
            subject=parsed.subject,
            sender=parsed.sender,
            recipients=parsed.recipients,
            cc=parsed.cc,
            date=parsed.date,
            message_header_id=parsed.message_header_id,
            references=parsed.references,
            snippet=_bounded_text(data.get('snippet'), MAX_SNIPPET_CHARS),
            label_ids=_string_tuple(data.get('labelIds')),
            body_text=parsed.body_text,
            attachments=parsed.attachments,
        )

    @staticmethod
    def _page_size(page_size: int) -> int:
        """Validate Gmail page size."""
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise GmailInputError('page size must be between 1 and 20')
        return page_size

    def search_messages(
        self,
        query: str,
        page_size: int,
        page_token: str | None,
    ) -> SearchMessagesResponse:
        """Search Gmail message summaries."""
        service = self._service()
        messages = service.users().messages()
        kwargs: dict[str, Any] = {
            'userId': USER_ID,
            'q': query,
            'maxResults': self._page_size(page_size),
        }
        if page_token is not None:
            kwargs['pageToken'] = page_token
        page = self._execute(messages.list(**kwargs))
        raw_items = page.get('messages') or ()
        if not isinstance(raw_items, Sequence):
            raise GmailProviderError('Gmail returned an invalid response')
        items: list[MessageSummary] = []
        for raw in raw_items[:page_size]:
            value = _mapping(raw)
            message_id = _bounded_text(value.get('id'), 256)
            metadata = self._execute(
                messages.get(
                    userId=USER_ID,
                    id=message_id,
                    format='metadata',
                    metadataHeaders=list(METADATA_HEADERS),
                )
            )
            items.append(self._message_summary(metadata))
        estimate = page.get('resultSizeEstimate')
        return SearchMessagesResponse(
            items=tuple(items),
            next_page_token=(
                _bounded_text(page['nextPageToken'], 2048)
                if page.get('nextPageToken')
                else None
            ),
            result_size_estimate=(
                int(estimate) if isinstance(estimate, int) else None
            ),
        )

    def search_threads(
        self,
        query: str,
        page_size: int,
        page_token: str | None,
    ) -> SearchThreadsResponse:
        """Search Gmail thread summaries."""
        service = self._service()
        threads = service.users().threads()
        kwargs: dict[str, Any] = {
            'userId': USER_ID,
            'q': query,
            'maxResults': self._page_size(page_size),
        }
        if page_token is not None:
            kwargs['pageToken'] = page_token
        page = self._execute(threads.list(**kwargs))
        raw_items = page.get('threads') or ()
        if not isinstance(raw_items, Sequence):
            raise GmailProviderError('Gmail returned an invalid response')
        items: list[ThreadSummary] = []
        for raw in raw_items[:page_size]:
            value = _mapping(raw)
            thread_id = _bounded_text(value.get('id'), 256)
            metadata = self._execute(
                threads.get(
                    userId=USER_ID,
                    id=thread_id,
                    format='metadata',
                    metadataHeaders=list(METADATA_HEADERS),
                )
            )
            messages_value = metadata.get('messages') or ()
            messages = (
                messages_value
                if isinstance(messages_value, Sequence)
                and not isinstance(messages_value, str | bytes)
                else ()
            )
            if len(messages) > MAX_THREAD_PROVIDER_MESSAGES:
                raise GmailProviderError('Gmail returned an invalid response')
            bounded_messages = messages[:MAX_THREAD_MESSAGES]
            summaries = tuple(
                self._message_summary(_mapping(message))
                for message in bounded_messages
            )
            first = summaries[0] if summaries else None
            labels = _thread_label_ids(messages)
            items.append(
                ThreadSummary(
                    thread_id=thread_id,
                    subject=first.subject if first else '',
                    sender=first.sender if first else '',
                    date=first.date if first else '',
                    snippet=_bounded_text(
                        metadata.get('snippet', value.get('snippet', '')),
                        MAX_SNIPPET_CHARS,
                    ),
                    label_ids=labels,
                    message_count=len(messages),
                )
            )
        estimate = page.get('resultSizeEstimate')
        return SearchThreadsResponse(
            items=tuple(items),
            next_page_token=(
                _bounded_text(page['nextPageToken'], 2048)
                if page.get('nextPageToken')
                else None
            ),
            result_size_estimate=(
                int(estimate) if isinstance(estimate, int) else None
            ),
        )

    def get_message(self, message_id: str) -> MessageDetail:
        """Get one Gmail message."""
        service = self._service()
        data = self._execute(
            service.users()
            .messages()
            .get(
                userId=USER_ID,
                id=message_id,
                format='full',
            )
        )
        return self._message_detail(data)

    def get_thread(self, thread_id: str) -> ThreadDetail:
        """Get one Gmail thread."""
        service = self._service()
        data = self._execute(
            service.users()
            .threads()
            .get(
                userId=USER_ID,
                id=thread_id,
                format='full',
            )
        )
        messages_value = data.get('messages') or ()
        if not isinstance(messages_value, Sequence) or isinstance(
            messages_value, str | bytes
        ):
            raise GmailProviderError('Gmail returned an invalid response')
        if len(messages_value) > MAX_THREAD_PROVIDER_MESSAGES:
            raise GmailProviderError('Gmail returned an invalid response')
        return ThreadDetail(
            thread_id=_bounded_text(data.get('id', thread_id), 256),
            history_id=(
                _bounded_text(data['historyId'], 256)
                if data.get('historyId')
                else None
            ),
            messages=tuple(
                self._message_detail(_mapping(message))
                for message in messages_value[:MAX_THREAD_MESSAGES]
            ),
        )

    def list_labels(self) -> LabelsResponse:
        """List Gmail labels."""
        service = self._service()
        data = self._execute(service.users().labels().list(userId=USER_ID))
        labels_value = data.get('labels') or ()
        if not isinstance(labels_value, Sequence):
            raise GmailProviderError('Gmail returned an invalid response')
        return LabelsResponse(
            items=tuple(
                LabelSummary(
                    label_id=_bounded_text(label.get('id'), 256),
                    name=_bounded_text(label.get('name')),
                    label_type=_bounded_text(label.get('type'), 64),
                    message_list_visibility=(
                        _bounded_text(label['messageListVisibility'], 64)
                        if label.get('messageListVisibility')
                        else None
                    ),
                    label_list_visibility=(
                        _bounded_text(label['labelListVisibility'], 64)
                        if label.get('labelListVisibility')
                        else None
                    ),
                )
                for label in (
                    _mapping(value) for value in labels_value[:MAX_LABELS]
                )
            )
        )

    def modify_labels(
        self,
        target_type: TargetType,
        target_id: str,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> MutationResult:
        """Modify Gmail target labels."""
        if not target_id or len(target_id) > 256:
            raise GmailInputError('target ID is invalid')

        def normalize(values: Sequence[str]) -> tuple[str, ...]:
            """Normalize Gmail label IDs."""
            if len(values) > MAX_LABEL_IDS:
                raise GmailInputError('too many label IDs')
            normalized: list[str] = []
            for value in values:
                label = value.strip()
                if not label or len(label) > 256:
                    raise GmailInputError('label ID is invalid')
                if label not in normalized:
                    normalized.append(label)
            return tuple(normalized)

        additions = normalize(add_label_ids)
        removals = normalize(remove_label_ids)
        if not additions and not removals:
            raise GmailInputError('at least one label change is required')
        if set(additions).intersection(removals):
            raise GmailInputError('label changes overlap')
        service = self._service()
        collection = (
            service.users().messages()
            if target_type is TargetType.MESSAGE
            else service.users().threads()
        )
        data = self._execute(
            collection.modify(
                userId=USER_ID,
                id=target_id,
                body={
                    'addLabelIds': list(additions),
                    'removeLabelIds': list(removals),
                },
            )
        )
        label_ids = (
            _string_tuple(data.get('labelIds'))
            if target_type is TargetType.MESSAGE
            else _thread_label_ids(data.get('messages'))
        )
        return MutationResult(
            target_type=target_type,
            target_id=_bounded_text(data.get('id', target_id), 256),
            label_ids=label_ids,
        )

    def archive(
        self, target_type: TargetType, target_id: str
    ) -> MutationResult:
        """Archive Gmail target."""
        return self.modify_labels(target_type, target_id, (), ('INBOX',))

    def mark_read(
        self, target_type: TargetType, target_id: str
    ) -> MutationResult:
        """Mark Gmail target read."""
        return self.modify_labels(target_type, target_id, (), ('UNREAD',))

    def mark_unread(
        self, target_type: TargetType, target_id: str
    ) -> MutationResult:
        """Mark Gmail target unread."""
        return self.modify_labels(target_type, target_id, ('UNREAD',), ())

    def get_attachment(
        self,
        message_id: str,
        attachment_id: str,
    ) -> AttachmentPayload:
        """Fetch Gmail attachment payload."""
        service = self._service()
        messages = service.users().messages()
        if attachment_id.startswith('inline:'):
            data = self._execute(
                messages.get(
                    userId=USER_ID,
                    id=message_id,
                    format='full',
                )
            )
            payload_value = data.get('payload')
            payload = (
                payload_value if isinstance(payload_value, Mapping) else {}
            )
            encoded, size = find_inline_attachment(
                payload, attachment_id.removeprefix('inline:')
            )
        else:
            data = self._execute(
                messages.attachments().get(
                    userId=USER_ID,
                    messageId=message_id,
                    id=attachment_id,
                )
            )
            encoded_value = data.get('data')
            size_value = data.get('size')
            if not isinstance(encoded_value, str) or not isinstance(
                size_value, int
            ):
                raise GmailProviderError('Gmail returned an invalid response')
            encoded = encoded_value
            size = size_value
        return AttachmentPayload(
            attachment_id=attachment_id,
            encoded_data=encoded,
            size=size,
        )

    def list_drafts(
        self,
        page_size: int,
        page_token: str | None,
    ) -> DraftsResponse:
        """List Gmail draft summaries."""
        service = self._service()
        kwargs: dict[str, Any] = {
            'userId': USER_ID,
            'maxResults': self._page_size(page_size),
        }
        if page_token is not None:
            kwargs['pageToken'] = page_token
        data = self._execute(service.users().drafts().list(**kwargs))
        drafts_value = data.get('drafts') or ()
        if not isinstance(drafts_value, Sequence):
            raise GmailProviderError('Gmail returned an invalid response')
        items = []
        for value in drafts_value[:page_size]:
            draft = _mapping(value)
            message = _mapping(draft.get('message', {}))
            items.append(
                DraftSummary(
                    draft_id=str(draft.get('id', '')),
                    message_id=str(message.get('id', '')),
                    thread_id=str(message.get('threadId', '')),
                )
            )
        estimate = data.get('resultSizeEstimate')
        return DraftsResponse(
            items=tuple(items),
            next_page_token=(
                _bounded_text(data['nextPageToken'], 2048)
                if data.get('nextPageToken')
                else None
            ),
            result_size_estimate=(
                int(estimate) if isinstance(estimate, int) else None
            ),
        )

    def get_draft(self, draft_id: str) -> DraftDetail:
        """Get one Gmail draft."""
        service = self._service()
        data = self._execute(
            service.users()
            .drafts()
            .get(
                userId=USER_ID,
                id=draft_id,
                format='full',
            )
        )
        return DraftDetail(
            draft_id=str(data.get('id', draft_id)),
            message=self._message_detail(_mapping(data.get('message', {}))),
        )

    def create_draft(
        self,
        to: Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
    ) -> DraftSummary:
        """Create one Gmail draft."""
        raw = build_plain_message(to, subject, body, cc=cc, bcc=bcc)
        service = self._service()
        data = self._execute(
            service.users()
            .drafts()
            .create(
                userId=USER_ID,
                body={'message': {'raw': raw}},
            )
        )
        message = _mapping(data.get('message', {}))
        return DraftSummary(
            draft_id=str(data.get('id', '')),
            message_id=str(message.get('id', '')),
            thread_id=str(message.get('threadId', '')),
        )

    def update_draft(
        self,
        draft_id: str,
        to: Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
    ) -> DraftSummary:
        """Update one Gmail draft."""
        raw = build_plain_message(to, subject, body, cc=cc, bcc=bcc)
        service = self._service()
        data = self._execute(
            service.users()
            .drafts()
            .update(
                userId=USER_ID,
                id=draft_id,
                body={'id': draft_id, 'message': {'raw': raw}},
            )
        )
        message = _mapping(data.get('message', {}))
        return DraftSummary(
            draft_id=str(data.get('id', draft_id)),
            message_id=str(message.get('id', '')),
            thread_id=str(message.get('threadId', '')),
        )

    def delete_draft(self, draft_id: str) -> DraftDeletion:
        """Delete one Gmail draft."""
        service = self._service()
        self._execute_empty(
            service.users().drafts().delete(userId=USER_ID, id=draft_id)
        )
        return DraftDeletion(draft_id=draft_id)

    def send_draft(self, draft_id: str) -> SentMessage:
        """Send one Gmail draft."""
        service = self._service()
        data = self._execute(
            service.users()
            .drafts()
            .send(
                userId=USER_ID,
                body={'id': draft_id},
            )
        )
        return self._sent_message(data)

    @staticmethod
    def _sent_message(data: Mapping[str, Any]) -> SentMessage:
        """Normalize sent Gmail message."""
        return SentMessage(
            message_id=str(data.get('id', '')),
            thread_id=str(data.get('threadId', '')),
            label_ids=_string_tuple(data.get('labelIds')),
        )

    def send_message(
        self,
        to: Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
    ) -> SentMessage:
        """Send one Gmail message."""
        raw = build_plain_message(to, subject, body, cc=cc, bcc=bcc)
        service = self._service()
        data = self._execute(
            service.users()
            .messages()
            .send(
                userId=USER_ID,
                body={'raw': raw},
            )
        )
        return self._sent_message(data)

    def reply_to_author(self, message_id: str, body: str) -> SentMessage:
        """Reply to Gmail author."""
        original = self.get_message(message_id)
        raw, thread_id = build_reply_message(original, body)
        service = self._service()
        data = self._execute(
            service.users()
            .messages()
            .send(
                userId=USER_ID,
                body={'raw': raw, 'threadId': thread_id},
            )
        )
        return self._sent_message(data)
