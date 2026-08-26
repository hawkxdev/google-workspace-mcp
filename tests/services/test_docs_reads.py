"""Test Docs bounded reads."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.services.docs.client import DocsGateway
from google_workspace_mcp.services.docs.constants import (
    MAX_DOCS_BLOCKS,
    MAX_DOCS_NODES,
    MAX_DOCS_OUTPUT_CHARS,
)
from google_workspace_mcp.services.docs.errors import DocsInputError
from google_workspace_mcp.services.docs.schemas import (
    DocsBlockKind,
    DocsElementKind,
    DocsParagraphBlock,
    DocsTableBlock,
)
from tests.services.docs_provider import (
    EMOJI,
    FakeDocsService,
    FakeDocsStore,
    document,
    document_with_nested_tabs,
    paragraph,
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


def lines_document(count: int, text: str = 'Line\n') -> dict[str, Any]:
    """Build many paragraph document."""
    width = len(text)
    body = [paragraph(1 + index * width, text) for index in range(count)]
    return document([tab('tab-1', body)])


def test_read_content_requires_explicit_tab_selection(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', document_with_nested_tabs())
    content = gateway.read_content('document-1', 'tab-3-1-1')
    assert content.tab_id == 'tab-3-1-1'


def test_read_content_projects_typed_paragraph_blocks(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    content = gateway.read_content('document-1', 'tab-1')
    assert content.blocks[0].kind is DocsBlockKind.SECTION_BREAK
    assert content.blocks[1].kind is DocsBlockKind.PARAGRAPH
    assert content.blocks[1].elements[0].content == 'Hello\n'


def test_read_content_propagates_document_revision(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document(revision_id='revision-77'))
    content = gateway.read_content('document-1', 'tab-1')
    assert content.revision_id == 'revision-77'
    assert content.document_id == 'document-1'


def test_read_content_returns_typed_table(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    payload = document([tab('tab-1', [table(1, ('Left\n', 'Right\n'))])])
    fake_service.queue('get', payload)
    content = gateway.read_content('document-1', 'tab-1')
    block = content.blocks[0]
    assert block.kind is DocsBlockKind.TABLE
    assert block.rows[0].cells[0].blocks[0].elements[0].content == 'Left\n'


def test_read_content_marks_unsupported_elements(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    marker = {
        'startIndex': 6,
        'endIndex': 7,
        'inlineObjectElement': {'inlineObjectId': 'object-1'},
    }
    payload = document([tab('tab-1', [paragraph(1, 'Hello', tail=(marker,))])])
    fake_service.queue('get', payload)
    content = gateway.read_content('document-1', 'tab-1')
    assert content.unsupported_kinds == ('inline_object',)
    assert content.blocks[0].elements[1].kind is DocsElementKind.INLINE_OBJECT


def test_read_content_applies_default_block_cap(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', lines_document(MAX_DOCS_BLOCKS + 10))
    content = gateway.read_content('document-1', 'tab-1')
    assert len(content.blocks) == MAX_DOCS_BLOCKS
    assert content.truncated is True


def test_read_content_reports_next_start_index_of_omitted_block(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', lines_document(5))
    content = gateway.read_content('document-1', 'tab-1', max_blocks=2)
    assert len(content.blocks) == 2
    assert content.next_start_index == content.blocks[-1].end_index
    assert content.next_start_index == 11


def test_read_content_applies_character_cap(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', lines_document(5))
    content = gateway.read_content('document-1', 'tab-1', max_chars=8)
    assert content.truncated is True
    assert content.text_characters <= 8
    assert content.next_start_index is not None


def test_read_content_is_not_truncated_when_complete(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    content = gateway.read_content('document-1', 'tab-1')
    assert content.truncated is False
    assert content.next_start_index is None


def test_read_content_narrows_to_requested_range(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    content = gateway.read_content(
        'document-1', 'tab-1', start_index=7, end_index=13
    )
    assert len(content.blocks) == 1
    assert content.blocks[0].start_index == 7


def test_read_content_rejects_reversed_range(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='range'):
        gateway.read_content('document-1', 'tab-1', start_index=9, end_index=3)


def test_read_content_rejects_negative_range(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsInputError, match='range'):
        gateway.read_content('document-1', 'tab-1', start_index=-1)


@pytest.mark.parametrize('max_blocks', [0, -1, MAX_DOCS_BLOCKS + 1])
def test_read_content_rejects_out_of_bound_block_cap(
    gateway: DocsGateway, max_blocks: int
) -> None:
    with pytest.raises(DocsInputError, match='max_blocks'):
        gateway.read_content('document-1', 'tab-1', max_blocks=max_blocks)


@pytest.mark.parametrize('max_chars', [0, -1, MAX_DOCS_OUTPUT_CHARS + 1])
def test_read_content_rejects_out_of_bound_character_cap(
    gateway: DocsGateway, max_chars: int
) -> None:
    with pytest.raises(DocsInputError, match='max_chars'):
        gateway.read_content('document-1', 'tab-1', max_chars=max_chars)


def test_out_of_bound_cap_never_reaches_provider(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError):
        gateway.read_content('document-1', 'tab-1', max_blocks=0)
    assert fake_service.documents_endpoint.calls == []


@pytest.mark.parametrize('tab_id', ['', '   ', 'a\x00b'])
def test_read_content_rejects_invalid_tab_identifier(
    fake_service: FakeDocsService, gateway: DocsGateway, tab_id: str
) -> None:
    with pytest.raises(DocsInputError, match='Tab ID'):
        gateway.read_content('document-1', tab_id)
    assert fake_service.documents_endpoint.calls == []


def test_single_oversized_paragraph_is_clipped_to_cap(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    oversized = 'x' * (MAX_DOCS_OUTPUT_CHARS + 1)
    fake_service.queue(
        'get', document([tab('tab-1', [paragraph(1, oversized)])])
    )
    content = gateway.read_content('document-1', 'tab-1')
    assert content.text_characters == MAX_DOCS_OUTPUT_CHARS
    assert content.truncated is True
    assert content.next_start_index == 1 + MAX_DOCS_OUTPUT_CHARS


def empty_cell_table(start: int, count: int) -> dict[str, Any]:
    """Build table of empty cells."""
    cells = [
        {
            'startIndex': start + 1 + index,
            'endIndex': start + 2 + index,
            'content': [],
        }
        for index in range(count)
    ]
    return {
        'startIndex': start,
        'endIndex': start + count + 2,
        'table': {
            'rows': 1,
            'columns': count,
            'tableRows': [
                {
                    'startIndex': start + 1,
                    'endIndex': start + count + 1,
                    'tableCells': cells,
                }
            ],
        },
    }


def single_cell_rows_table(start: int, rows: int) -> dict[str, Any]:
    """Build table of empty rows."""
    table_rows = [
        {
            'startIndex': start + 1 + index * 2,
            'endIndex': start + 3 + index * 2,
            'tableCells': [],
        }
        for index in range(rows)
    ]
    return {
        'startIndex': start,
        'endIndex': start + rows * 2 + 2,
        'table': {'rows': rows, 'columns': 0, 'tableRows': table_rows},
    }


def test_cell_count_is_bounded_by_node_budget(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    count = MAX_DOCS_NODES + 50
    fake_service.queue(
        'get', document([tab('tab-1', [empty_cell_table(1, count)])])
    )
    content = gateway.read_content('document-1', 'tab-1')
    projected = sum(
        len(row.cells)
        for block in content.blocks
        if isinstance(block, DocsTableBlock)
        for row in block.rows
    )
    assert content.truncated is True
    assert projected == MAX_DOCS_NODES - 1
    assert content.next_start_index is not None


def test_row_count_is_bounded_by_node_budget(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    rows = MAX_DOCS_NODES + 50
    fake_service.queue(
        'get', document([tab('tab-1', [single_cell_rows_table(1, rows)])])
    )
    content = gateway.read_content('document-1', 'tab-1')
    projected = sum(
        len(block.rows)
        for block in content.blocks
        if isinstance(block, DocsTableBlock)
    )
    assert content.truncated is True
    assert projected == MAX_DOCS_NODES
    assert content.next_start_index is not None


def test_clip_never_splits_a_surrogate_pair(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    text = EMOJI * 40
    fake_service.queue('get', document([tab('tab-1', [paragraph(1, text)])]))
    content = gateway.read_content('document-1', 'tab-1', max_chars=5)
    kept = ''.join(
        element.content or ''
        for block in content.blocks
        if isinstance(block, DocsParagraphBlock)
        for element in block.elements
    )
    assert content.truncated is True
    assert kept == EMOJI * 2
    assert content.text_characters == 4
