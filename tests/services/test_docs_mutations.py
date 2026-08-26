"""Test Docs revision mutations."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.services.docs.client import DocsGateway
from google_workspace_mcp.services.docs.constants import MAX_DOCS_TEXT_CHARS
from google_workspace_mcp.services.docs.errors import (
    DocsConflictError,
    DocsInputError,
    DocsNotFoundError,
    DocsProviderError,
)
from tests.services.docs_provider import (
    EMOJI,
    FakeDocsService,
    FakeDocsStore,
    batch_result,
    created_document,
    document,
    paragraph,
    replace_reply,
    simple_document,
    tab,
    table,
)


@pytest.fixture
def fake_service() -> FakeDocsService:
    """Create fake Docs service."""
    return FakeDocsService()


@pytest.fixture
def gateway(fake_service: FakeDocsService) -> DocsGateway:
    """Create gateway under test."""
    return DocsGateway(FakeDocsStore(), service_builder=lambda _: fake_service)


def stage(
    fake_service: FakeDocsService,
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Queue preflight and mutation."""
    fake_service.queue(
        'get', payload if payload is not None else simple_document()
    )
    fake_service.queue(
        'batchUpdate', result if result is not None else batch_result()
    )


def emoji_document() -> dict[str, Any]:
    """Build document containing emoji."""
    return document([tab('tab-1', [paragraph(1, f'A{EMOJI}B\n')])])


# Insert


def test_insert_sends_required_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    result = gateway.insert_text(
        'document-1',
        'tab-1',
        1,
        'Hello',
        required_revision_id='revision-1',
    )
    assert fake_service.last_write_control == {
        'requiredRevisionId': 'revision-1'
    }
    assert result.required_revision_id == 'revision-2'


def test_insert_uses_tab_scoped_location(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    gateway.insert_text(
        'document-1', 'tab-1', 1, 'Hello', required_revision_id='revision-1'
    )
    assert fake_service.last_requests == [
        {
            'insertText': {
                'text': 'Hello',
                'location': {'index': 1, 'tabId': 'tab-1'},
            }
        }
    ]


def test_insert_reports_document_and_tab(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    result = gateway.insert_text(
        'document-1', 'tab-1', 1, 'Hello', required_revision_id='revision-1'
    )
    assert result.document_id == 'document-1'
    assert result.tab_id == 'tab-1'


def test_insert_rejects_stale_revision_before_mutation(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document(revision_id='revision-7'))
    with pytest.raises(DocsConflictError, match='revision'):
        gateway.insert_text(
            'document-1',
            'tab-1',
            1,
            'Hello',
            required_revision_id='revision-1',
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_insert_rejects_unknown_tab_before_mutation(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsNotFoundError, match='Docs tab was not found'):
        gateway.insert_text(
            'document-1',
            'tab-missing',
            1,
            'Hello',
            required_revision_id='revision-1',
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_insert_rejects_split_surrogate_index(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', emoji_document())
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        gateway.insert_text(
            'document-1',
            'tab-1',
            3,
            'Hello',
            required_revision_id='revision-1',
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_insert_accepts_boundary_after_surrogate_pair(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, emoji_document())
    result = gateway.insert_text(
        'document-1', 'tab-1', 4, 'Hello', required_revision_id='revision-1'
    )
    assert result.required_revision_id == 'revision-2'


def test_insert_rejects_index_outside_segment(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='segment'):
        gateway.insert_text(
            'document-1',
            'tab-1',
            99,
            'Hello',
            required_revision_id='revision-1',
        )


@pytest.mark.parametrize('text', ['', 'a' * (MAX_DOCS_TEXT_CHARS + 1)])
def test_insert_rejects_invalid_text(
    fake_service: FakeDocsService, gateway: DocsGateway, text: str
) -> None:
    with pytest.raises(DocsInputError, match='text'):
        gateway.insert_text(
            'document-1',
            'tab-1',
            1,
            text,
            required_revision_id='revision-1',
        )
    assert fake_service.documents_endpoint.calls == []


def test_insert_rejects_missing_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='Revision ID'):
        gateway.insert_text(
            'document-1', 'tab-1', 1, 'Hello', required_revision_id=''
        )
    assert fake_service.documents_endpoint.calls == []


def test_insert_writes_without_outer_retry(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    gateway.insert_text(
        'document-1', 'tab-1', 1, 'Hello', required_revision_id='revision-1'
    )
    write_request = fake_service.documents_endpoint.calls[-1][2]
    assert write_request.retries == [0]


def test_missing_next_revision_fails_closed(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, result={'documentId': 'document-1', 'replies': []})
    with pytest.raises(DocsProviderError):
        gateway.insert_text(
            'document-1',
            'tab-1',
            1,
            'Hello',
            required_revision_id='revision-1',
        )


# Delete


def test_delete_range_uses_tab_scoped_range(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    gateway.delete_range(
        'document-1', 'tab-1', 1, 7, required_revision_id='revision-1'
    )
    assert fake_service.last_requests == [
        {
            'deleteContentRange': {
                'range': {
                    'startIndex': 1,
                    'endIndex': 7,
                    'tabId': 'tab-1',
                }
            }
        }
    ]


def test_delete_range_refuses_terminal_newline(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='terminal newline'):
        gateway.delete_range(
            'document-1', 'tab-1', 1, 13, required_revision_id='revision-1'
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_delete_range_accepts_up_to_terminal_newline(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    result = gateway.delete_range(
        'document-1', 'tab-1', 1, 12, required_revision_id='revision-1'
    )
    assert result.required_revision_id == 'revision-2'


def test_delete_range_rejects_reversed_bounds(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='range'):
        gateway.delete_range(
            'document-1', 'tab-1', 7, 3, required_revision_id='revision-1'
        )


def test_delete_range_rejects_split_surrogate(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', emoji_document())
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        gateway.delete_range(
            'document-1', 'tab-1', 1, 3, required_revision_id='revision-1'
        )


def test_delete_range_rejects_stale_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document(revision_id='revision-7'))
    with pytest.raises(DocsConflictError, match='revision'):
        gateway.delete_range(
            'document-1', 'tab-1', 1, 5, required_revision_id='revision-1'
        )
    assert fake_service.calls_for('batchUpdate') == []


# Create


def test_create_document_sends_only_title(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('create', created_document())
    gateway.create_document('New Document')
    assert fake_service.calls_for('create') == [
        {'body': {'title': 'New Document'}}
    ]


def test_create_document_returns_root_contract(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('create', created_document())
    result = gateway.create_document('New Document')
    assert result.document_id == 'document-9'
    assert result.title == 'New Document'
    assert result.required_revision_id == 'revision-1'
    assert result.tab_id == 'tab-1'


def test_create_document_resolves_tab_when_absent(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('create', created_document(with_tabs=False))
    fake_service.queue(
        'get',
        simple_document(document_id='document-9', revision_id='revision-1'),
    )
    result = gateway.create_document('New Document')
    assert result.tab_id == 'tab-1'
    assert result.required_revision_id == 'revision-1'


@pytest.mark.parametrize('title', ['', '   ', 'a\x00b', 'x' * 400])
def test_create_document_rejects_invalid_title(
    fake_service: FakeDocsService, gateway: DocsGateway, title: str
) -> None:
    with pytest.raises(DocsInputError, match='title'):
        gateway.create_document(title)
    assert fake_service.documents_endpoint.calls == []


def test_create_document_writes_without_outer_retry(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('create', created_document())
    gateway.create_document('New Document')
    assert fake_service.documents_endpoint.calls[0][2].retries == [0]


# Replace


def test_replace_text_sends_tab_criteria_and_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, result=batch_result(replies=(replace_reply(1),)))
    gateway.replace_text(
        'document-1',
        'tab-1',
        'Hello',
        'Goodbye',
        required_revision_id='revision-1',
        match_case=True,
        expected_occurrences=1,
    )
    assert fake_service.last_requests == [
        {
            'replaceAllText': {
                'containsText': {'text': 'Hello', 'matchCase': True},
                'replaceText': 'Goodbye',
                'tabsCriteria': {'tabIds': ['tab-1']},
            }
        }
    ]
    assert fake_service.last_write_control == {
        'requiredRevisionId': 'revision-1'
    }


def test_replace_text_returns_provider_occurrences(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, result=batch_result(replies=(replace_reply(1),)))
    result = gateway.replace_text(
        'document-1',
        'tab-1',
        'Hello',
        'Goodbye',
        required_revision_id='revision-1',
        match_case=True,
        expected_occurrences=1,
    )
    assert result.occurrences_changed == 1
    assert result.required_revision_id == 'revision-2'


def test_replace_text_rejects_expected_count_mismatch(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='expected_occurrences'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            'Hello',
            'Goodbye',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=3,
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_replace_text_counts_case_sensitively(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='expected_occurrences'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            'hello',
            'Goodbye',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=1,
        )


def test_replace_text_counts_case_insensitively(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, result=batch_result(replies=(replace_reply(1),)))
    result = gateway.replace_text(
        'document-1',
        'tab-1',
        'hello',
        'Goodbye',
        required_revision_id='revision-1',
        match_case=False,
        expected_occurrences=1,
    )
    assert result.occurrences_changed == 1


def test_replace_text_counts_inside_table_cells(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    payload = document([tab('tab-1', [table(1, ('Left\n', 'Left\n'))])])
    stage(fake_service, payload, batch_result(replies=(replace_reply(2),)))
    result = gateway.replace_text(
        'document-1',
        'tab-1',
        'Left',
        'Right',
        required_revision_id='revision-1',
        match_case=True,
        expected_occurrences=2,
    )
    assert result.occurrences_changed == 2


@pytest.mark.parametrize(
    'literal', ['', 'a\nb', 'x' * (MAX_DOCS_TEXT_CHARS + 1)]
)
def test_replace_text_rejects_invalid_literal(
    fake_service: FakeDocsService, gateway: DocsGateway, literal: str
) -> None:
    with pytest.raises(DocsInputError, match='search text'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            literal,
            'Goodbye',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=1,
        )
    assert fake_service.documents_endpoint.calls == []


def test_replace_text_rejects_match_across_structural_boundary(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='expected_occurrences'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            'HelloWorld',
            'Merged',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=1,
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_replace_text_rejects_match_across_unsupported_element(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    marker = {
        'startIndex': 3,
        'endIndex': 4,
        'inlineObjectElement': {'inlineObjectId': 'object-1'},
    }
    body = [
        {
            'startIndex': 1,
            'endIndex': 6,
            'paragraph': {
                'elements': [
                    {
                        'startIndex': 1,
                        'endIndex': 3,
                        'textRun': {'content': 'AB', 'textStyle': {}},
                    },
                    marker,
                    {
                        'startIndex': 4,
                        'endIndex': 6,
                        'textRun': {'content': 'CD', 'textStyle': {}},
                    },
                ],
                'paragraphStyle': {'namedStyleType': 'NORMAL_TEXT'},
            },
        }
    ]
    fake_service.queue('get', document([tab('tab-1', body)]))
    with pytest.raises(DocsInputError, match='expected_occurrences'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            'ABCD',
            'Merged',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=1,
        )


@pytest.mark.parametrize('expected', [-1, 0, 5000])
def test_replace_text_rejects_out_of_bound_expectation(
    fake_service: FakeDocsService, gateway: DocsGateway, expected: int
) -> None:
    with pytest.raises(DocsInputError, match='expected_occurrences'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            'Hello',
            'Goodbye',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=expected,
        )
    assert fake_service.documents_endpoint.calls == []


def test_replace_text_rejects_stale_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document(revision_id='revision-7'))
    with pytest.raises(DocsConflictError, match='revision'):
        gateway.replace_text(
            'document-1',
            'tab-1',
            'Hello',
            'Goodbye',
            required_revision_id='revision-1',
            match_case=True,
            expected_occurrences=1,
        )
    assert fake_service.calls_for('batchUpdate') == []
