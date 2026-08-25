"""Normalize Gmail MIME messages."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser
from typing import Any

from .constants import (
    MAX_ATTACHMENTS,
    MAX_BODY_CHARS,
    MAX_FILENAME_CHARS,
    MAX_HEADER_CHARS,
    MAX_MIME_DEPTH,
    MAX_MIME_HEADERS,
    MAX_MIME_PARTS,
    MAX_MIME_SOURCE_CHARS,
    MAX_OUTBOUND_BODY_BYTES,
    MAX_RAW_MESSAGE_BYTES,
    MAX_SUBJECT_BYTES,
)
from .errors import GmailInputError, GmailPayloadError
from .schemas import AttachmentSummary, MessageDetail, ParsedMessage

_HIDDEN_HTML_TAGS = frozenset(
    {'head', 'noscript', 'script', 'style', 'template', 'title'}
)


class _TextExtractor(HTMLParser):
    """Extract visible HTML text."""

    def __init__(self) -> None:
        """Initialize HTML text extractor."""
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._hidden_classes: set[str] = set()
        self._hidden_ids: set[str] = set()

    @staticmethod
    def _hidden_style(value: str) -> bool:
        """Detect hidden inline style."""
        style = re.sub(r'\s+', '', value.casefold())
        direct_markers = (
            'display:none',
            'visibility:hidden',
            'content-visibility:hidden',
            'opacity:0',
            'font-size:0',
            'clip:rect(0',
            'clip-path:inset(100%',
            'left:-9999',
            'text-indent:-9999',
        )
        if any(marker in style for marker in direct_markers):
            return True
        zero_width = 'width:0' in style or 'max-width:0' in style
        zero_height = 'height:0' in style or 'max-height:0' in style
        return 'overflow:hidden' in style and zero_width and zero_height

    def _record_hidden_styles(self, value: str) -> None:
        """Record hidden CSS selectors."""
        for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', value):
            if not self._hidden_style(match.group(2)):
                continue
            for selector in match.group(1).split(','):
                normalized = selector.strip()
                class_match = re.fullmatch(r'\.([A-Za-z0-9_-]+)', normalized)
                id_match = re.fullmatch(r'#([A-Za-z0-9_-]+)', normalized)
                if class_match:
                    self._hidden_classes.add(class_match.group(1))
                if id_match:
                    self._hidden_ids.add(id_match.group(1))

    def _hidden_attributes(self, attrs: list[tuple[str, str | None]]) -> bool:
        """Detect hidden HTML attributes."""
        values = {name.casefold(): value or '' for name, value in attrs}
        classes = values.get('class', '').split()
        return (
            'hidden' in values
            or values.get('aria-hidden', '').casefold() == 'true'
            or self._hidden_style(values.get('style', ''))
            or any(value in self._hidden_classes for value in classes)
            or values.get('id', '') in self._hidden_ids
        )

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track HTML element visibility."""
        normalized = tag.casefold()
        parent_hidden = self._stack[-1][1] if self._stack else False
        hidden = (
            parent_hidden
            or normalized in _HIDDEN_HTML_TAGS
            or self._hidden_attributes(attrs)
        )
        self._stack.append((normalized, hidden))

    def handle_endtag(self, tag: str) -> None:
        """Close matched HTML element."""
        normalized = tag.casefold()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == normalized:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        """Collect visible HTML data."""
        if self._stack and self._stack[-1][0] == 'style':
            self._record_hidden_styles(data)
            return
        value = data.strip()
        hidden = self._stack[-1][1] if self._stack else False
        if value and not hidden:
            self.values.append(value)

    def text(self) -> str:
        """Return normalized visible text."""
        return ' '.join(self.values)


def decode_base64url(data: str) -> bytes:
    """Decode Gmail base64 payload."""
    try:
        encoded = data.encode('ascii')
        encoded += b'=' * (-len(encoded) % 4)
        return base64.b64decode(encoded, altchars=b'-_', validate=True)
    except UnicodeEncodeError, binascii.Error, ValueError:
        raise GmailPayloadError('invalid message encoding') from None


def encode_base64url(data: bytes) -> str:
    """Encode Gmail base64 payload."""
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def header_value(
    headers: Sequence[Mapping[str, Any]],
    name: str,
) -> str | None:
    """Read case insensitive header."""
    expected = name.casefold()
    for header in headers:
        if str(header.get('name', '')).casefold() == expected:
            return str(header.get('value', ''))[:MAX_HEADER_CHARS]
    return None


def _addresses(value: str | None) -> tuple[str, ...]:
    """Normalize message address list."""
    if not value:
        return ()
    return tuple(
        address[:320] for _, address in getaddresses([value])[:100] if address
    )


def _validated_recipients(values: Sequence[str]) -> tuple[str, ...]:
    """Validate outgoing recipient list."""
    if len(values) > 100:
        raise GmailInputError('too many message recipients')
    normalized: list[str] = []
    for value in values:
        if (
            not value
            or len(value) > 320
            or any(ord(character) < 32 for character in value)
            or (
                '<' not in value
                and '>' not in value
                and any(character.isspace() for character in value)
            )
        ):
            raise GmailInputError('message recipient is invalid')
        try:
            addresses = getaddresses([value], strict=True)
        except ValueError:
            raise GmailInputError('message recipient is invalid') from None
        if len(addresses) != 1:
            raise GmailInputError('message recipient is invalid')
        display_name, address = addresses[0]
        local, separator, domain = address.rpartition('@')
        if (
            separator != '@'
            or not local
            or not domain
            or any(character.isspace() for character in address)
        ):
            raise GmailInputError('message recipient is invalid')
        normalized.append(formataddr((display_name, address)))
    return tuple(normalized)


def _html_text(value: str) -> str:
    """Convert HTML into text."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _decode_bounded_text(
    data: str,
    remaining: int,
    *,
    source_multiplier: int = 1,
) -> str:
    """Decode bounded MIME text."""
    if remaining <= 0:
        return ''
    source_limit = remaining * source_multiplier
    encoded_limit = 4 * ((source_limit + 2) // 3)
    decoded = decode_base64url(data[:encoded_limit]).decode(
        'utf-8', errors='replace'
    )[:source_limit]
    return decoded if source_multiplier > 1 else decoded[:remaining]


def parse_message(
    payload: Mapping[str, Any],
    *,
    max_chars: int = MAX_BODY_CHARS,
    max_depth: int = MAX_MIME_DEPTH,
) -> ParsedMessage:
    """Parse normalized Gmail message."""
    if max_chars < 0 or max_depth < 0:
        raise GmailInputError('message limits must be nonnegative')
    if max_depth != MAX_MIME_DEPTH:
        global_limit = MAX_MIME_DEPTH
        if max_depth < global_limit:
            global_limit = max_depth
    else:
        global_limit = MAX_MIME_DEPTH
    plain: list[str] = []
    html: list[str] = []
    attachments: list[AttachmentSummary] = []
    parts_seen = 0
    source_chars_seen = 0

    def collect(part: Mapping[str, Any], depth: int) -> None:
        """Collect parts with local limit."""
        nonlocal parts_seen, source_chars_seen
        parts_seen += 1
        if parts_seen > MAX_MIME_PARTS:
            raise GmailPayloadError('message has too many MIME parts')
        if depth > global_limit:
            raise GmailPayloadError('message structure is too deep')
        mime_type = str(part.get('mimeType', '')).casefold()
        filename = str(part.get('filename', ''))
        body = part.get('body')
        body_mapping = body if isinstance(body, Mapping) else {}
        attachment_id = str(body_mapping.get('attachmentId', ''))
        part_id = str(part.get('partId', ''))
        data = body_mapping.get('data')
        size_value = body_mapping.get('size', 0)
        try:
            size = max(int(size_value), 0)
        except TypeError, ValueError:
            raise GmailPayloadError('invalid attachment size') from None
        if filename or attachment_id:
            effective_id = attachment_id
            if (
                not effective_id
                and filename
                and isinstance(data, str)
                and part_id
            ):
                effective_id = f'inline:{part_id}'
            if effective_id and len(attachments) < MAX_ATTACHMENTS:
                attachments.append(
                    AttachmentSummary(
                        attachment_id=effective_id[:MAX_HEADER_CHARS],
                        filename=(filename or 'attachment')[
                            :MAX_FILENAME_CHARS
                        ],
                        mime_type=mime_type[:MAX_HEADER_CHARS],
                        size=size,
                    )
                )
            return
        if isinstance(data, str) and data:
            source_chars_seen += len(data)
            if source_chars_seen > MAX_MIME_SOURCE_CHARS:
                raise GmailPayloadError('message MIME source is too large')
            target = plain if mime_type == 'text/plain' else html
            if mime_type in {'text/plain', 'text/html'}:
                used = sum(len(value) for value in target)
                remaining = max_chars - used - (2 * len(target))
                decoded = _decode_bounded_text(
                    data,
                    remaining,
                    source_multiplier=(30 if mime_type == 'text/html' else 1),
                )
                if decoded:
                    target.append(decoded)
        children = part.get('parts')
        if isinstance(children, Sequence) and not isinstance(
            children, str | bytes
        ):
            for child in children:
                if not isinstance(child, Mapping):
                    raise GmailPayloadError('invalid message structure')
                collect(child, depth + 1)

    collect(payload, 0)
    headers_value = payload.get('headers')
    headers = (
        headers_value
        if isinstance(headers_value, Sequence)
        and not isinstance(headers_value, str | bytes)
        else ()
    )
    if len(headers) > MAX_MIME_HEADERS:
        raise GmailPayloadError('message has too many headers')
    normalized_headers = tuple(
        header for header in headers if isinstance(header, Mapping)
    )
    if plain:
        body_text = '\n\n'.join(plain)
    else:
        body_text = '\n\n'.join(_html_text(value) for value in html)
    return ParsedMessage(
        subject=header_value(normalized_headers, 'Subject') or '',
        sender=header_value(normalized_headers, 'From') or '',
        recipients=_addresses(header_value(normalized_headers, 'To')),
        cc=_addresses(header_value(normalized_headers, 'Cc')),
        date=header_value(normalized_headers, 'Date') or '',
        message_header_id=header_value(normalized_headers, 'Message-ID') or '',
        references=header_value(normalized_headers, 'References') or '',
        body_text=body_text[:max_chars],
        attachments=tuple(attachments),
    )


def find_inline_attachment(
    payload: Mapping[str, Any],
    part_id: str,
    *,
    max_depth: int = MAX_MIME_DEPTH,
) -> tuple[str, int]:
    """Find inline Gmail attachment."""
    parts_seen = 0

    def find(part: Mapping[str, Any], depth: int) -> tuple[str, int] | None:
        """Search nested Gmail parts."""
        nonlocal parts_seen
        parts_seen += 1
        if parts_seen > MAX_MIME_PARTS:
            raise GmailPayloadError('message has too many MIME parts')
        if depth > max_depth:
            raise GmailPayloadError('message structure is too deep')
        if str(part.get('partId', '')) == part_id:
            body = part.get('body')
            body_mapping = body if isinstance(body, Mapping) else {}
            data = body_mapping.get('data')
            size = body_mapping.get('size')
            if isinstance(data, str) and isinstance(size, int):
                return data, size
        children = part.get('parts')
        if isinstance(children, Sequence) and not isinstance(
            children, str | bytes
        ):
            for child in children:
                if not isinstance(child, Mapping):
                    raise GmailPayloadError('invalid message structure')
                found = find(child, depth + 1)
                if found is not None:
                    return found
        return None

    result = find(payload, 0)
    if result is None:
        raise GmailPayloadError('inline attachment was not found')
    return result


def _validate_outbound_text(subject: str, body: str) -> tuple[str, str]:
    """Validate outbound message text."""
    normalized_subject = subject.strip()
    if not normalized_subject:
        raise GmailInputError('message subject is required')
    if len(normalized_subject.encode('utf-8')) > MAX_SUBJECT_BYTES:
        raise GmailInputError('message subject is too large')
    if not body:
        raise GmailInputError('message body is required')
    if len(body.encode('utf-8')) > MAX_OUTBOUND_BODY_BYTES:
        raise GmailInputError('message body is too large')
    return normalized_subject, body


def _encode_outbound_message(message: EmailMessage) -> str:
    """Encode bounded outbound message."""
    raw = message.as_bytes()
    if len(raw) > MAX_RAW_MESSAGE_BYTES:
        raise GmailInputError('message is too large')
    return encode_base64url(raw)


def build_plain_message(
    to: Sequence[str],
    subject: str,
    body: str,
    *,
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
) -> str:
    """Build plain Gmail message."""
    recipients = _validated_recipients(to)
    copied = _validated_recipients(cc)
    blind_copied = _validated_recipients(bcc)
    if not recipients:
        raise GmailInputError('at least one recipient is required')
    if len(recipients) + len(copied) + len(blind_copied) > 100:
        raise GmailInputError('too many message recipients')
    normalized_subject, normalized_body = _validate_outbound_text(
        subject, body
    )
    message = EmailMessage()
    message['To'] = ', '.join(recipients)
    if copied:
        message['Cc'] = ', '.join(copied)
    if blind_copied:
        message['Bcc'] = ', '.join(blind_copied)
    message['Subject'] = normalized_subject
    message.set_content(normalized_body)
    return _encode_outbound_message(message)


def build_reply_message(original: MessageDetail, body: str) -> tuple[str, str]:
    """Build threaded Gmail reply."""
    try:
        sender = _validated_recipients((original.sender,))[0]
    except GmailInputError:
        raise GmailPayloadError(
            'original message cannot be replied to'
        ) from None
    if not original.message_header_id or not original.thread_id:
        raise GmailPayloadError('original message cannot be replied to')
    subject = re.sub(r'^(?:\s*re:\s*)+', '', original.subject, flags=re.I)
    reply_subject, reply_body = _validate_outbound_text(f'Re: {subject}', body)
    message = EmailMessage()
    message['To'] = sender
    message['Subject'] = reply_subject
    message['In-Reply-To'] = original.message_header_id
    references = original.references.strip()
    message['References'] = ' '.join(
        value for value in (references, original.message_header_id) if value
    )
    message.set_content(reply_body)
    return _encode_outbound_message(message), original.thread_id
