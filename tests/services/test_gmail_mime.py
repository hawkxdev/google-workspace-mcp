"""Test Gmail MIME normalization."""

from __future__ import annotations

import base64
from email import message_from_bytes, policy

import pytest

from google_workspace_mcp.services.gmail.errors import (
    GmailInputError,
    GmailPayloadError,
)
from google_workspace_mcp.services.gmail.mime import (
    build_plain_message,
    build_reply_message,
    decode_base64url,
    parse_message,
)
from google_workspace_mcp.services.gmail.schemas import MessageDetail


def _encoded(value: str) -> str:
    """Encode Gmail payload text."""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip('=')


def test_decode_base64url_restores_padding() -> None:
    assert decode_base64url(_encoded('hello')) == b'hello'
    with pytest.raises(GmailPayloadError, match='invalid message encoding'):
        decode_base64url('%%%')


def test_parse_message_prefers_plain_and_bounds_html_fallback() -> None:
    payload = {
        'mimeType': 'multipart/alternative',
        'headers': [{'name': 'sUbJeCt', 'value': 'Nested'}],
        'parts': [
            {
                'mimeType': 'text/html',
                'body': {'data': _encoded('<p>html <b>body</b></p>')},
            },
            {
                'mimeType': 'text/plain',
                'body': {'data': _encoded('plain body')},
            },
        ],
    }
    parsed = parse_message(payload, max_chars=8)
    assert parsed.subject == 'Nested'
    assert parsed.body_text == 'plain bo'

    html_only = parse_message(
        {
            'mimeType': 'text/html',
            'headers': [],
            'body': {
                'data': _encoded(
                    '<head>metadata</head><script>secret</script>'
                    '<style>.concealed{display:none}#gone{visibility:hidden}</style>'
                    '<div hidden>attribute secret</div>'
                    '<div aria-hidden="true">aria secret</div>'
                    '<div style="display: none">style secret</div>'
                    '<div style="opacity: 0">opacity secret</div>'
                    '<div class="concealed">class secret</div>'
                    '<div id="gone">id secret</div>'
                    '<p>Hello <b>world</b></p>'
                )
            },
        },
        max_chars=20,
    )
    assert html_only.body_text == 'Hello world'


def test_parse_message_collects_attachment_metadata() -> None:
    parsed = parse_message(
        {
            'mimeType': 'multipart/mixed',
            'headers': [],
            'parts': [
                {
                    'mimeType': 'application/pdf',
                    'filename': '../invoice.pdf',
                    'body': {'attachmentId': 'att-1', 'size': 42},
                },
                {
                    'partId': 'part-2',
                    'mimeType': 'image/png',
                    'filename': 'image.png',
                    'body': {'data': _encoded('png'), 'size': 3},
                },
            ],
        }
    )
    assert parsed.attachments[0].attachment_id == 'att-1'
    assert parsed.attachments[0].filename == '../invoice.pdf'
    assert parsed.attachments[0].size == 42
    assert parsed.attachments[1].attachment_id == 'inline:part-2'


def test_build_plain_and_reply_messages() -> None:
    encoded = build_plain_message(
        ('alice@example.com',),
        'Subject',
        'Body',
        cc=('copy@example.com',),
    )
    message = message_from_bytes(
        decode_base64url(encoded), policy=policy.default
    )
    assert message['To'] == 'alice@example.com'
    assert message['Cc'] == 'copy@example.com'
    assert message['Subject'] == 'Subject'
    assert message.get_content().strip() == 'Body'
    assert not message.is_multipart()

    original = MessageDetail(
        message_id='message-1',
        thread_id='thread-1',
        subject='Topic',
        sender='Alice <alice@example.com>',
        recipients=('owner@example.com',),
        cc=(),
        date='2026-08-24T10:00:00Z',
        message_header_id='<parent@example.com>',
        references='<root@example.com>',
        snippet='snippet',
        label_ids=(),
        body_text='original',
        attachments=(),
    )
    reply_raw, thread_id = build_reply_message(original, 'Reply body')
    reply = message_from_bytes(
        decode_base64url(reply_raw), policy=policy.default
    )
    assert thread_id == 'thread-1'
    assert reply['To'] == 'Alice <alice@example.com>'
    assert reply['Subject'] == 'Re: Topic'
    assert reply['In-Reply-To'] == '<parent@example.com>'
    assert reply['References'] == '<root@example.com> <parent@example.com>'
    assert reply.get_content().strip() == 'Reply body'


def test_composition_rejects_missing_required_values() -> None:
    with pytest.raises(GmailInputError, match='recipient'):
        build_plain_message((), 'Subject', 'Body')
    with pytest.raises(GmailInputError, match='subject'):
        build_plain_message(('alice@example.com',), '', 'Body')
    with pytest.raises(GmailInputError, match='body'):
        build_plain_message(('alice@example.com',), 'Subject', '')

    incomplete = MessageDetail(
        message_id='message-1',
        thread_id='',
        subject='Topic',
        sender='',
    )
    with pytest.raises(GmailPayloadError, match='cannot be replied'):
        build_reply_message(incomplete, 'Reply')


def test_composition_rejects_multiple_mailboxes_per_item() -> None:
    with pytest.raises(GmailInputError, match='recipient is invalid'):
        build_plain_message(
            ('alice@example.com,bob@example.com',), 'Subject', 'Body'
        )


def test_parse_message_rejects_excessive_depth() -> None:
    payload: dict[str, object] = {
        'mimeType': 'text/plain',
        'body': {'data': _encoded('body')},
    }
    for _ in range(5):
        payload = {'mimeType': 'multipart/mixed', 'parts': [payload]}
    with pytest.raises(GmailPayloadError, match='too deep'):
        parse_message(payload, max_depth=3)


def test_parse_message_bounds_provider_collections() -> None:
    parts = [
        {
            'partId': f'part-{index}',
            'mimeType': 'application/octet-stream',
            'filename': 'f' * 400,
            'body': {'attachmentId': f'a-{index}', 'size': 1},
        }
        for index in range(120)
    ]
    parsed = parse_message(
        {
            'mimeType': 'multipart/mixed',
            'headers': [{'name': 'Subject', 'value': 's' * 3000}],
            'parts': parts,
        }
    )
    assert len(parsed.subject) == 2000
    assert len(parsed.attachments) == 100
    assert all(len(item.filename) == 255 for item in parsed.attachments)


@pytest.mark.parametrize('limit', range(1, 9))
def test_plain_text_truncation_uses_valid_base64_chunks(limit: int) -> None:
    parsed = parse_message(
        {
            'mimeType': 'text/plain',
            'body': {'data': _encoded('abcdefghijklmnopqrstuvwxyz')},
        },
        max_chars=limit,
    )
    assert parsed.body_text == 'abcdefghijklmnopqrstuvwxyz'[:limit]


@pytest.mark.parametrize(
    'recipient',
    ['not-an-email', 'alice@', '@example.com', 'alice @example.com'],
)
def test_composition_rejects_malformed_mailbox(recipient: str) -> None:
    with pytest.raises(GmailInputError, match='recipient is invalid'):
        build_plain_message((recipient,), 'Subject', 'Body')


def test_reply_rejects_malformed_sender() -> None:
    original = MessageDetail(
        message_id='m1',
        thread_id='t1',
        subject='Topic',
        sender='not-an-email',
        message_header_id='<parent@example.com>',
    )
    with pytest.raises(GmailPayloadError, match='cannot be replied'):
        build_reply_message(original, 'Reply body')


def test_outbound_message_enforces_encoded_size_limits() -> None:
    with pytest.raises(GmailInputError, match='subject is too large'):
        build_plain_message(('alice@example.com',), 'ü' * 600, 'Body')
    with pytest.raises(GmailInputError, match='body is too large'):
        build_plain_message(('alice@example.com',), 'Subject', 'x' * 100_001)

    original = MessageDetail(
        message_id='m1',
        thread_id='t1',
        subject='Topic',
        sender='alice@example.com',
        message_header_id='<parent@example.com>',
    )
    with pytest.raises(GmailInputError, match='body is required'):
        build_reply_message(original, '')


def test_parse_message_rejects_aggregate_provider_overflow() -> None:
    parts = [
        {'partId': str(index), 'mimeType': 'application/octet-stream'}
        for index in range(501)
    ]
    with pytest.raises(GmailPayloadError, match='too many MIME parts'):
        parse_message({'mimeType': 'multipart/mixed', 'parts': parts})

    headers = [
        {'name': f'X-{index}', 'value': 'value'} for index in range(101)
    ]
    with pytest.raises(GmailPayloadError, match='too many headers'):
        parse_message(
            {'mimeType': 'text/plain', 'headers': headers, 'body': {}}
        )
