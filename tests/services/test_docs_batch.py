"""Test Docs typed batch."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.services.docs.client import DocsGateway
from google_workspace_mcp.services.docs.constants import (
    MAX_DOCS_BATCH_OPERATIONS,
)
from google_workspace_mcp.services.docs.errors import (
    DocsConflictError,
    DocsInputError,
    DocsProviderError,
)
from google_workspace_mcp.services.docs.schemas import (
    DocsAlignment,
    DocsBatchOperationType,
    DocsBulletPreset,
    DocsNamedStyle,
)
from tests.services.docs_provider import (
    FakeDocsService,
    FakeDocsStore,
    FakeRequest,
    batch_result,
    make_http_error,
    replace_reply,
    simple_document,
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
    replies: tuple[dict[str, Any], ...] = ({},),
) -> None:
    """Queue preflight and batch."""
    fake_service.queue('get', simple_document())
    fake_service.queue('batchUpdate', batch_result(replies=replies))


def insert(index: int = 1, text: str = 'Hello') -> dict[str, Any]:
    """Build insert text operation."""
    return {
        'operation': 'insert_text',
        'index': index,
        'text': text,
    }


def run(gateway: DocsGateway, operations: list[Any]) -> Any:
    """Run batch with defaults."""
    return gateway.batch_update(
        'document-1',
        'tab-1',
        operations,
        required_revision_id='revision-1',
    )


# Operation inventory


def test_batch_operation_values_are_exactly_eight() -> None:
    assert {item.value for item in DocsBatchOperationType} == {
        'insert_text',
        'delete_range',
        'replace_text',
        'insert_page_break',
        'update_text_style',
        'update_paragraph_style',
        'create_bullets',
        'delete_bullets',
    }


def test_named_style_allowlist_is_closed() -> None:
    assert {item.value for item in DocsNamedStyle} == {
        'title',
        'subtitle',
        'normal_text',
        'heading_1',
        'heading_2',
        'heading_3',
        'heading_4',
        'heading_5',
        'heading_6',
    }


def test_alignment_allowlist_is_closed() -> None:
    assert {item.value for item in DocsAlignment} == {
        'start',
        'center',
        'end',
        'justified',
    }


def test_bullet_presets_are_closed() -> None:
    assert len(set(DocsBulletPreset)) >= 3
    assert all(
        item.value.islower() or '_' in item.value for item in DocsBulletPreset
    )


# Schema rejection


def test_unknown_operation_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(gateway, [{'operation': 'delete_table', 'index': 1}])
    assert fake_service.documents_endpoint.calls == []


def test_table_mutation_operation_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(gateway, [{'operation': 'insert_table_row', 'index': 1}])
    assert fake_service.documents_endpoint.calls == []


def test_raw_provider_request_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(gateway, [{'insertText': {'text': 'Hello'}}])
    assert fake_service.documents_endpoint.calls == []


def test_raw_field_mask_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(
            gateway,
            [
                {
                    'operation': 'update_text_style',
                    'start_index': 1,
                    'end_index': 5,
                    'bold': True,
                    'fields': 'bold',
                }
            ],
        )
    assert fake_service.documents_endpoint.calls == []


def test_raw_tab_identifier_inside_operation_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    operation = insert()
    operation['tab_id'] = 'tab-9'
    with pytest.raises(DocsInputError, match='operation'):
        run(gateway, [operation])
    assert fake_service.documents_endpoint.calls == []


def test_unsupported_named_style_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(
            gateway,
            [
                {
                    'operation': 'update_paragraph_style',
                    'start_index': 1,
                    'end_index': 5,
                    'named_style': 'banner',
                }
            ],
        )
    assert fake_service.documents_endpoint.calls == []


def test_unsupported_text_style_field_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(
            gateway,
            [
                {
                    'operation': 'update_text_style',
                    'start_index': 1,
                    'end_index': 5,
                    'bold': True,
                    'font_size': 24,
                }
            ],
        )
    assert fake_service.documents_endpoint.calls == []


def test_text_style_without_any_field_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='operation'):
        run(
            gateway,
            [
                {
                    'operation': 'update_text_style',
                    'start_index': 1,
                    'end_index': 5,
                }
            ],
        )
    assert fake_service.documents_endpoint.calls == []


def test_batch_rejects_more_than_twenty_operations(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    operations = [
        insert(index=1, text='x') for _ in range(MAX_DOCS_BATCH_OPERATIONS + 1)
    ]
    with pytest.raises(DocsInputError, match='at most 20'):
        run(gateway, operations)
    assert fake_service.documents_endpoint.calls == []


def test_batch_rejects_empty_operation_list(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='at least one'):
        run(gateway, [])
    assert fake_service.documents_endpoint.calls == []


def test_batch_rejects_missing_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError, match='Revision ID'):
        gateway.batch_update(
            'document-1', 'tab-1', [insert()], required_revision_id=''
        )
    assert fake_service.documents_endpoint.calls == []


def test_batch_rejects_stale_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document(revision_id='revision-7'))
    with pytest.raises(DocsConflictError, match='revision'):
        run(gateway, [insert()])
    assert fake_service.calls_for('batchUpdate') == []


# Ordering and atomicity


def test_batch_preserves_caller_order(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, replies=({}, {}))
    run(
        gateway,
        [
            insert(index=1, text='AB'),
            {
                'operation': 'update_text_style',
                'start_index': 1,
                'end_index': 3,
                'bold': True,
            },
        ],
    )
    requests = fake_service.last_requests
    assert list(requests[0]) == ['insertText']
    assert list(requests[1]) == ['updateTextStyle']


def test_batch_sends_single_write_control(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, replies=({}, {}))
    run(gateway, [insert(index=1), insert(index=1)])
    assert fake_service.last_write_control == {
        'requiredRevisionId': 'revision-1'
    }
    assert len(fake_service.calls_for('batchUpdate')) == 1


def test_batch_reports_operation_count_and_next_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, replies=({}, {}))
    result = run(gateway, [insert(index=1), insert(index=1)])
    assert result.operation_count == 2
    assert result.required_revision_id == 'revision-2'
    assert result.document_id == 'document-1'
    assert result.tab_id == 'tab-1'


def test_provider_failure_raises_one_fixed_error(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    fake_service.queue(
        'batchUpdate', FakeRequest(error=make_http_error(400, 'badRequest'))
    )
    with pytest.raises(DocsProviderError) as caught:
        run(gateway, [insert(index=1), insert(index=1)])
    assert 'badRequest' not in str(caught.value)
    assert 'secret123' not in str(caught.value)


def test_reply_count_mismatch_fails_closed(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, replies=({},))
    with pytest.raises(DocsProviderError):
        run(gateway, [insert(index=1), insert(index=1)])


# Request mapping


def test_insert_operation_inherits_top_level_tab(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(gateway, [insert(index=1, text='Hello')])
    assert fake_service.last_requests == [
        {
            'insertText': {
                'text': 'Hello',
                'location': {'index': 1, 'tabId': 'tab-1'},
            }
        }
    ]


def test_page_break_operation_maps_to_provider(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(gateway, [{'operation': 'insert_page_break', 'index': 1}])
    assert fake_service.last_requests == [
        {'insertPageBreak': {'location': {'index': 1, 'tabId': 'tab-1'}}}
    ]


def test_delete_range_operation_maps_to_provider(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(
        gateway,
        [{'operation': 'delete_range', 'start_index': 1, 'end_index': 7}],
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


def test_text_style_operation_builds_owned_field_mask(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(
        gateway,
        [
            {
                'operation': 'update_text_style',
                'start_index': 1,
                'end_index': 5,
                'bold': True,
                'italic': False,
            }
        ],
    )
    request = fake_service.last_requests[0]['updateTextStyle']
    assert request['textStyle'] == {'bold': True, 'italic': False}
    assert request['fields'] == 'bold,italic'
    assert request['range'] == {
        'startIndex': 1,
        'endIndex': 5,
        'tabId': 'tab-1',
    }


def test_paragraph_style_operation_maps_allowlisted_values(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(
        gateway,
        [
            {
                'operation': 'update_paragraph_style',
                'start_index': 1,
                'end_index': 5,
                'named_style': 'heading_2',
                'alignment': 'center',
            }
        ],
    )
    request = fake_service.last_requests[0]['updateParagraphStyle']
    assert request['paragraphStyle'] == {
        'namedStyleType': 'HEADING_2',
        'alignment': 'CENTER',
    }
    assert request['fields'] == 'namedStyleType,alignment'


def test_create_bullets_operation_uses_closed_preset(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(
        gateway,
        [
            {
                'operation': 'create_bullets',
                'start_index': 1,
                'end_index': 5,
                'preset': 'disc_circle_square',
            }
        ],
    )
    request = fake_service.last_requests[0]['createParagraphBullets']
    assert request['bulletPreset'] == 'BULLET_DISC_CIRCLE_SQUARE'
    assert request['range']['tabId'] == 'tab-1'


def test_delete_bullets_operation_maps_to_provider(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    run(
        gateway,
        [{'operation': 'delete_bullets', 'start_index': 1, 'end_index': 5}],
    )
    assert 'deleteParagraphBullets' in fake_service.last_requests[0]


def test_replace_operation_reuses_paragraph_preflight(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service, replies=(replace_reply(1),))
    result = run(
        gateway,
        [
            {
                'operation': 'replace_text',
                'search_text': 'Hello',
                'replacement_text': 'Goodbye',
                'match_case': True,
                'expected_occurrences': 1,
            }
        ],
    )
    assert result.replies[0].occurrences_changed == 1
    request = fake_service.last_requests[0]['replaceAllText']
    assert request['tabsCriteria'] == {'tabIds': ['tab-1']}


def test_replace_operation_rejects_count_mismatch(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='expected_occurrences'):
        run(
            gateway,
            [
                {
                    'operation': 'replace_text',
                    'search_text': 'Hello',
                    'replacement_text': 'Goodbye',
                    'match_case': True,
                    'expected_occurrences': 4,
                }
            ],
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_replace_operation_rejects_newline_literal(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError):
        run(
            gateway,
            [
                {
                    'operation': 'replace_text',
                    'search_text': 'Hello\nWorld',
                    'replacement_text': 'Merged',
                    'match_case': True,
                    'expected_occurrences': 1,
                }
            ],
        )
    assert fake_service.documents_endpoint.calls == []


# Range guards against the initial structure


def test_batch_rejects_index_outside_segment(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='segment'):
        run(gateway, [insert(index=99)])
    assert fake_service.calls_for('batchUpdate') == []


def test_batch_rejects_terminal_newline_deletion(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='terminal newline'):
        run(
            gateway,
            [
                {
                    'operation': 'delete_range',
                    'start_index': 1,
                    'end_index': 13,
                }
            ],
        )
    assert fake_service.calls_for('batchUpdate') == []


def test_batch_validates_every_operation_before_sending(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError):
        run(gateway, [insert(index=1), insert(index=99)])
    assert fake_service.calls_for('batchUpdate') == []


def test_batch_style_range_may_touch_terminal_newline(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    stage(fake_service)
    result = run(
        gateway,
        [
            {
                'operation': 'update_paragraph_style',
                'start_index': 1,
                'end_index': 13,
                'named_style': 'heading_1',
            }
        ],
    )
    assert result.operation_count == 1
