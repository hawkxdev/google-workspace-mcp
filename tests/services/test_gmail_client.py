"""Test Gmail provider gateway."""

from __future__ import annotations

from email import message_from_bytes, policy
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from google_workspace_mcp.services.gmail.client import GmailGateway
from google_workspace_mcp.services.gmail.errors import (
    GmailInputError,
    GmailProviderError,
)
from google_workspace_mcp.services.gmail.mime import decode_base64url
from google_workspace_mcp.services.gmail.schemas import TargetType

from .conftest import FakeCredentialStore, FakeGmailService, FakeRequest


def _metadata(
    message_id: str,
    thread_id: str,
    subject: str,
) -> dict[str, Any]:
    """Build provider metadata response."""
    return {
        'id': message_id,
        'threadId': thread_id,
        'labelIds': ['INBOX'],
        'snippet': f'{subject} snippet',
        'payload': {
            'headers': [
                {'name': 'Subject', 'value': subject},
                {'name': 'From', 'value': 'alice@example.com'},
                {'name': 'Date', 'value': 'Sun, 24 Aug 2026 10:00:00 +0000'},
            ]
        },
    }


def test_search_messages_refreshes_and_uses_native_retry(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    messages = gmail_service.users_resource.message_collection
    messages.queue(
        'list',
        {
            'messages': [
                {'id': 'm1', 'threadId': 't1'},
                {'id': 'm2', 'threadId': 't2'},
            ],
            'nextPageToken': 'next-token',
            'resultSizeEstimate': 99,
        },
    )
    messages.queue('get', _metadata('m1', 't1', 'One'))
    messages.queue('get', _metadata('m2', 't2', 'Two'))
    built_credentials: list[Any] = []

    def builder(credentials: Any) -> FakeGmailService:
        """Record service credentials."""
        built_credentials.append(credentials)
        return gmail_service

    gateway = GmailGateway(credential_store, service_builder=builder)
    result = gateway.search_messages('from:alice', 2, 'page-token')

    assert credential_store.calls == 1
    assert built_credentials == [credential_store.credentials]
    assert [item.subject for item in result.items] == ['One', 'Two']
    assert result.next_page_token == 'next-token'
    assert result.result_size_estimate == 99
    assert messages.calls[0][1] == {
        'userId': 'me',
        'q': 'from:alice',
        'maxResults': 2,
        'pageToken': 'page-token',
    }
    assert all(call[2].retries == [2] for call in messages.calls)


def test_search_messages_normalizes_missing_collection(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    gmail_service.users_resource.message_collection.queue('list', {})
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    result = gateway.search_messages('', 10, None)
    assert result.items == ()
    assert result.next_page_token is None
    assert result.result_size_estimate is None


def test_provider_error_is_sanitized(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    marker = 'provider-secret-marker'
    response = httplib2.Response({'status': '403'})
    error = HttpError(
        response,
        ('{"error":{"message":"' + marker + '"}}').encode(),
        uri='https://gmail.googleapis.test/messages?q=' + marker,
    )
    gmail_service.users_resource.message_collection.queue(
        'list', FakeRequest(error=error)
    )
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    with pytest.raises(GmailProviderError) as captured:
        gateway.search_messages(marker, 10, None)
    assert marker not in str(captured.value)
    assert str(captured.value) == 'Gmail request was forbidden'


def test_rate_limit_reason_is_classified_without_payload_leak(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    marker = 'rate-provider-marker'
    response = httplib2.Response({'status': '403'})
    content = (
        '{"error":{"errors":[{"reason":'
        '"userRateLimitExceeded","message":"' + marker + '"}]}}'
    ).encode()
    request = FakeRequest(error=HttpError(response, content, uri=marker))
    gmail_service.users_resource.message_collection.queue('list', request)
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    with pytest.raises(GmailProviderError, match='rate limited') as captured:
        gateway.search_messages('', 10, None)
    assert marker not in str(captured.value)


def test_label_and_draft_requests_use_exact_contracts(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    messages = gmail_service.users_resource.message_collection
    threads = gmail_service.users_resource.thread_collection
    drafts = gmail_service.users_resource.draft_collection
    messages.queue('modify', {'id': 'm1', 'labelIds': ['STARRED']})
    threads.queue(
        'modify',
        {
            'id': 't1',
            'messages': [
                {'id': 'm1', 'labelIds': ['STARRED']},
                {'id': 'm2', 'labelIds': ['INBOX', 'STARRED']},
            ],
        },
    )
    drafts.queue(
        'create',
        {'id': 'd1', 'message': {'id': 'm2', 'threadId': 't2'}},
    )
    drafts.queue('list', {})
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )

    mutation = gateway.modify_labels(
        TargetType.MESSAGE,
        'm1',
        ('STARRED', 'STARRED'),
        ('UNREAD',),
    )
    assert mutation.label_ids == ('STARRED',)
    assert messages.calls[0][1]['body'] == {
        'addLabelIds': ['STARRED'],
        'removeLabelIds': ['UNREAD'],
    }
    thread_mutation = gateway.modify_labels(
        TargetType.THREAD, 't1', ('STARRED',), ()
    )
    assert thread_mutation.label_ids == ('STARRED', 'INBOX')

    draft = gateway.create_draft(
        ('alice@example.com',),
        'Subject',
        'Body',
    )
    assert draft.draft_id == 'd1'
    request_body = drafts.calls[0][1]['body']
    raw = request_body['message']['raw']
    parsed = message_from_bytes(decode_base64url(raw), policy=policy.default)
    assert parsed['To'] == 'alice@example.com'
    assert parsed.get_content().strip() == 'Body'
    assert 'attachments' not in request_body['message']

    page = gateway.list_drafts(10, None)
    assert page.items == ()


def test_send_and_reply_preserve_thread_headers(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    messages = gmail_service.users_resource.message_collection
    messages.queue('send', {'id': 'sent-1', 'threadId': 'thread-new'})
    messages.queue(
        'get',
        {
            'id': 'original',
            'threadId': 'thread-1',
            'payload': {
                'mimeType': 'text/plain',
                'headers': [
                    {'name': 'Subject', 'value': 'Topic'},
                    {'name': 'From', 'value': 'Alice <alice@example.com>'},
                    {'name': 'Message-ID', 'value': '<parent@example.com>'},
                    {'name': 'References', 'value': '<root@example.com>'},
                ],
                'body': {'data': ''},
            },
        },
    )
    messages.queue('send', {'id': 'sent-2', 'threadId': 'thread-1'})
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )

    sent = gateway.send_message(
        ('bob@example.com',),
        'Hello',
        'New body',
    )
    assert sent.message_id == 'sent-1'

    reply = gateway.reply_to_author('original', 'Reply body')
    assert reply.thread_id == 'thread-1'
    reply_body = messages.calls[-1][1]['body']
    assert reply_body['threadId'] == 'thread-1'
    parsed = message_from_bytes(
        decode_base64url(reply_body['raw']),
        policy=policy.default,
    )
    assert parsed['To'] == 'Alice <alice@example.com>'
    assert parsed['In-Reply-To'] == '<parent@example.com>'
    assert parsed['References'] == '<root@example.com> <parent@example.com>'


def test_inline_attachment_uses_message_payload(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    messages = gmail_service.users_resource.message_collection
    messages.queue(
        'get',
        {
            'id': 'm1',
            'payload': {
                'mimeType': 'multipart/mixed',
                'parts': [
                    {
                        'partId': 'part-1',
                        'filename': 'small.txt',
                        'mimeType': 'text/plain',
                        'body': {'data': 'c21hbGw', 'size': 5},
                    }
                ],
            },
        },
    )
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    payload = gateway.get_attachment('m1', 'inline:part-1')
    assert payload.encoded_data == 'c21hbGw'
    assert payload.size == 5
    assert gmail_service.users_resource.attachment_collection.calls == []


def test_thread_and_label_reads_normalize_provider_data(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    threads = gmail_service.users_resource.thread_collection
    labels = gmail_service.users_resource.label_collection
    threads.queue(
        'list',
        {'threads': [{'id': 't1', 'snippet': 'thread'}]},
    )
    threads.queue(
        'get',
        {
            'id': 't1',
            'messages': [_metadata('m1', 't1', 'Topic')],
        },
    )
    threads.queue(
        'get',
        {
            'id': 't1',
            'historyId': 'h1',
            'messages': [_metadata('m1', 't1', 'Topic')],
        },
    )
    labels.queue(
        'list',
        {'labels': [{'id': 'INBOX', 'name': 'Inbox', 'type': 'system'}]},
    )
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )

    search = gateway.search_threads('', 10, None)
    assert search.items[0].subject == 'Topic'
    assert search.items[0].label_ids == ('INBOX',)
    thread = gateway.get_thread('t1')
    assert thread.history_id == 'h1'
    assert thread.messages[0].message_id == 'm1'
    response = gateway.list_labels()
    assert response.items[0].label_id == 'INBOX'


def test_complete_draft_lifecycle_uses_native_requests(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    drafts = gmail_service.users_resource.draft_collection
    draft_message = _metadata('m1', 't1', 'Draft')
    drafts.queue('get', {'id': 'd1', 'message': draft_message})
    drafts.queue(
        'update',
        {'id': 'd1', 'message': {'id': 'm1', 'threadId': 't1'}},
    )
    drafts.queue('delete', None)
    drafts.queue('send', {'id': 'm1', 'threadId': 't1', 'labelIds': ['SENT']})
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )

    assert gateway.get_draft('d1').message.subject == 'Draft'
    updated = gateway.update_draft(
        'd1', ('alice@example.com',), 'Updated', 'Body'
    )
    assert updated.draft_id == 'd1'
    assert gateway.delete_draft('d1').draft_id == 'd1'
    sent = gateway.send_draft('d1')
    assert sent.label_ids == ('SENT',)
    assert all(request.retries == [2] for _, _, request in drafts.calls)


def test_transport_failure_is_not_outer_retried(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    request = FakeRequest(error=ConnectionError('transport marker'))
    gmail_service.users_resource.message_collection.queue('list', request)
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    with pytest.raises(GmailProviderError, match='temporarily unavailable'):
        gateway.search_messages('', 10, None)
    assert request.retries == [2]


def test_label_changes_reject_empty_and_overlap(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    with pytest.raises(GmailInputError, match='label ID is invalid'):
        gateway.modify_labels(TargetType.MESSAGE, 'm1', ('',), ())
    with pytest.raises(GmailInputError, match='overlap'):
        gateway.modify_labels(
            TargetType.MESSAGE, 'm1', ('STARRED',), ('STARRED',)
        )


def test_thread_output_caps_message_collection(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    messages = [
        _metadata(f'm{index}', 't1', f'Subject {index}') for index in range(60)
    ]
    gmail_service.users_resource.thread_collection.queue(
        'get', {'id': 't1', 'messages': messages}
    )
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    thread = gateway.get_thread('t1')
    assert len(thread.messages) == 50
    assert thread.messages[-1].message_id == 'm49'


def test_thread_output_rejects_provider_collection_overflow(
    credential_store: FakeCredentialStore,
    gmail_service: FakeGmailService,
) -> None:
    messages = [
        _metadata(f'm{index}', 't1', f'Subject {index}')
        for index in range(101)
    ]
    gmail_service.users_resource.thread_collection.queue(
        'get', {'id': 't1', 'messages': messages}
    )
    gateway = GmailGateway(
        credential_store,
        service_builder=lambda _: gmail_service,
    )
    with pytest.raises(GmailProviderError, match='invalid response'):
        gateway.get_thread('t1')
