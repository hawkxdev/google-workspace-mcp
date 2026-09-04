"""Test Docs structure projection."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.services.docs.constants import (
    MAX_DOCS_BLOCK_DEPTH,
    MAX_DOCS_TAB_DEPTH,
    MAX_DOCS_TABS,
)
from google_workspace_mcp.services.docs.errors import (
    DocsInputError,
    DocsNotFoundError,
    DocsProviderError,
    DocsUnsupportedError,
)
from google_workspace_mcp.services.docs.schemas import (
    DocsBlockKind,
    DocsElementKind,
)
from google_workspace_mcp.services.docs.structure import (
    build_tab_segment,
    count_paragraph_matches,
    parse_document_tabs,
    project_tab_content,
    validate_delete_range,
)
from tests.services.docs_provider import (
    EMOJI,
    deep_tab_document,
    document,
    document_with_nested_tabs,
    nested_table_document,
    paragraph,
    section_break,
    simple_body,
    tab,
    table,
    table_of_contents,
    text_run,
)


def test_nested_tabs_are_preserved() -> None:
    parsed = parse_document_tabs(document_with_nested_tabs())
    assert parsed.tabs[0].children[0].children[0].tab_id == 'tab-3-1-1'


def test_tab_tree_records_parent_and_nesting() -> None:
    parsed = parse_document_tabs(document_with_nested_tabs())
    middle = parsed.tabs[0].children[0]
    assert middle.parent_tab_id == 'tab-3'
    assert middle.nesting_level == 1
    assert middle.child_count == 1
    assert parsed.tabs[0].parent_tab_id is None


def test_tab_summary_reports_body_bounds() -> None:
    parsed = parse_document_tabs(document([tab('tab-1', simple_body())]))
    assert parsed.tabs[0].start_index == 0
    assert parsed.tabs[0].end_index == 13


def test_document_summary_returns_identity_and_revision() -> None:
    parsed = parse_document_tabs(document([tab('tab-1', simple_body())]))
    assert parsed.document_id == 'document-1'
    assert parsed.title == 'Test Document'
    assert parsed.revision_id == 'revision-1'


def test_legacy_top_level_body_is_never_parsed() -> None:
    payload = document([tab('tab-1', simple_body())])
    payload['body'] = {'content': [paragraph(1, 'Legacy body\n')]}
    parsed = parse_document_tabs(payload)
    content = project_tab_content(payload, 'tab-1')
    rendered = ''.join(
        element.content or ''
        for block in content.blocks
        if block.kind is DocsBlockKind.PARAGRAPH
        for element in block.elements
    )
    assert len(parsed.tabs) == 1
    assert 'Legacy body' not in rendered


def test_document_without_tabs_is_rejected() -> None:
    payload = document([tab('tab-1', simple_body())])
    del payload['tabs']
    with pytest.raises(DocsProviderError, match='Docs response is invalid'):
        parse_document_tabs(payload)


def test_document_with_empty_tabs_is_rejected() -> None:
    with pytest.raises(DocsProviderError, match='Docs response is invalid'):
        parse_document_tabs(document([]))


def test_tab_without_identifier_is_rejected() -> None:
    node = tab('tab-1', simple_body())
    del node['tabProperties']['tabId']
    with pytest.raises(DocsProviderError, match='Docs response is invalid'):
        parse_document_tabs(document([node]))


def test_missing_revision_is_rejected() -> None:
    payload = document([tab('tab-1', simple_body())])
    del payload['revisionId']
    with pytest.raises(DocsProviderError, match='Docs response is invalid'):
        parse_document_tabs(payload)


def test_oversized_tab_count_is_rejected() -> None:
    nodes = [
        tab(f'tab-{number}', simple_body(), index=number)
        for number in range(MAX_DOCS_TABS + 1)
    ]
    with pytest.raises(DocsUnsupportedError, match='more than 200 tabs'):
        parse_document_tabs(document(nodes))


def test_excessive_tab_depth_is_rejected() -> None:
    node = tab('tab-deep', simple_body(), nesting_level=MAX_DOCS_TAB_DEPTH)
    for level in range(MAX_DOCS_TAB_DEPTH, 0, -1):
        node = tab(
            f'tab-{level}',
            simple_body(),
            nesting_level=level - 1,
            children=[node],
        )
    with pytest.raises(
        DocsUnsupportedError, match='nests tabs deeper than 10 levels'
    ):
        parse_document_tabs(document([node]))


def test_limit_refusal_is_not_reported_as_a_broken_provider_reply() -> None:
    nodes = [
        tab(f'tab-{number}', simple_body(), index=number)
        for number in range(MAX_DOCS_TABS + 1)
    ]
    broken = document([tab('tab-1', simple_body())])
    del broken['revisionId']

    with pytest.raises(DocsUnsupportedError) as limit:
        parse_document_tabs(document(nodes))
    with pytest.raises(DocsProviderError) as malformed:
        parse_document_tabs(broken)

    assert not isinstance(limit.value, DocsProviderError)
    assert not isinstance(malformed.value, DocsUnsupportedError)
    assert str(MAX_DOCS_TABS) in str(limit.value)
    assert str(MAX_DOCS_TABS) not in str(malformed.value)


def test_paragraph_block_preserves_text_runs_and_style() -> None:
    content = project_tab_content(
        document([tab('tab-1', simple_body())]), 'tab-1'
    )
    block = content.blocks[1]
    assert block.kind is DocsBlockKind.PARAGRAPH
    assert block.named_style == 'NORMAL_TEXT'
    assert block.start_index == 1
    assert block.end_index == 7
    assert block.elements[0].kind is DocsElementKind.TEXT_RUN
    assert block.elements[0].content == 'Hello\n'


def test_bullet_marker_is_reported() -> None:
    body = [
        paragraph(
            1,
            'Item\n',
            bullet={'listId': 'list-1', 'nestingLevel': 2},
        )
    ]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    block = content.blocks[0]
    assert block.kind is DocsBlockKind.PARAGRAPH
    assert block.bullet is not None
    assert block.bullet.list_id == 'list-1'
    assert block.bullet.nesting_level == 2


def test_section_break_and_contents_table_are_typed() -> None:
    body = [section_break(0), table_of_contents(1, 5)]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    assert content.blocks[0].kind is DocsBlockKind.SECTION_BREAK
    assert content.blocks[1].kind is DocsBlockKind.TABLE_OF_CONTENTS


def test_table_is_typed_and_not_flattened() -> None:
    body = [table(1, ('Left', 'Right'))]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    block = content.blocks[0]
    assert block.kind is DocsBlockKind.TABLE
    assert block.row_count == 1
    assert block.column_count == 2
    cell = block.rows[0].cells[1]
    inner = cell.blocks[0]
    assert inner.kind is DocsBlockKind.PARAGRAPH
    assert inner.elements[0].content == 'Right\n'


def test_unsupported_paragraph_element_is_marked() -> None:
    marker = {
        'startIndex': 6,
        'endIndex': 7,
        'inlineObjectElement': {'inlineObjectId': 'object-1'},
    }
    body = [paragraph(1, 'Hello', tail=(marker,))]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    element = content.blocks[0].elements[1]
    assert element.kind is DocsElementKind.INLINE_OBJECT
    assert element.content is None
    assert element.start_index == 6
    assert element.end_index == 7
    assert 'inline_object' in content.unsupported_kinds


def test_unknown_paragraph_element_is_marked_unknown() -> None:
    marker = {'startIndex': 6, 'endIndex': 7, 'quantumElement': {}}
    body = [paragraph(1, 'Hello', tail=(marker,))]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    element = content.blocks[0].elements[1]
    assert element.kind is DocsElementKind.UNKNOWN
    assert 'unknown' in content.unsupported_kinds


def test_unknown_structural_block_is_marked_unsupported() -> None:
    body = [{'startIndex': 1, 'endIndex': 4, 'quantumBlock': {}}]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    block = content.blocks[0]
    assert block.kind is DocsBlockKind.UNSUPPORTED
    assert block.start_index == 1
    assert block.end_index == 4
    assert 'unknown' in content.unsupported_kinds


def test_equation_and_page_break_report_distinct_kinds() -> None:
    body = [
        paragraph(
            1,
            'Hello',
            tail=(
                {'startIndex': 6, 'endIndex': 7, 'equation': {}},
                {'startIndex': 7, 'endIndex': 8, 'pageBreak': {}},
            ),
        )
    ]
    content = project_tab_content(document([tab('tab-1', body)]), 'tab-1')
    kinds = [element.kind for element in content.blocks[0].elements]
    assert DocsElementKind.EQUATION in kinds
    assert DocsElementKind.PAGE_BREAK in kinds
    assert set(content.unsupported_kinds) == {'equation', 'page_break'}


def test_block_cap_truncates_and_reports_next_start_index() -> None:
    body = [paragraph(1 + index * 6, 'Line\n') for index in range(5)]
    content = project_tab_content(
        document([tab('tab-1', body)]), 'tab-1', max_blocks=2
    )
    assert len(content.blocks) == 2
    assert content.truncated is True
    assert content.next_start_index == 13


def test_character_cap_truncates_projection() -> None:
    body = [paragraph(1 + index * 6, 'Line\n') for index in range(5)]
    content = project_tab_content(
        document([tab('tab-1', body)]), 'tab-1', max_chars=8
    )
    assert content.truncated is True
    assert len(content.blocks) < 5
    assert content.next_start_index is not None


def test_start_index_inside_a_surrogate_pair_is_rejected() -> None:
    body = [paragraph(1, f'{EMOJI}tail\n')]
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        project_tab_content(
            document([tab('tab-1', body)]), 'tab-1', start_index=2
        )


def test_start_index_after_a_surrogate_pair_keeps_true_coordinates() -> None:
    body = [paragraph(1, f'{EMOJI}tail\n')]
    content = project_tab_content(
        document([tab('tab-1', body)]), 'tab-1', start_index=3
    )
    element = content.blocks[0].elements[0]
    assert element.content == 'tail\n'
    assert element.start_index == 3
    assert element.end_index == 8


def test_complete_projection_is_not_truncated() -> None:
    content = project_tab_content(
        document([tab('tab-1', simple_body())]), 'tab-1'
    )
    assert content.truncated is False
    assert content.next_start_index is None


def test_requested_range_narrows_projection() -> None:
    content = project_tab_content(
        document([tab('tab-1', simple_body())]),
        'tab-1',
        start_index=7,
        end_index=13,
    )
    assert len(content.blocks) == 1
    assert content.blocks[0].start_index == 7


def test_unknown_tab_raises_not_found() -> None:
    with pytest.raises(DocsNotFoundError, match='Docs tab was not found'):
        project_tab_content(document([tab('tab-1', simple_body())]), 'tab-9')


def test_child_tab_content_is_reachable() -> None:
    content = project_tab_content(document_with_nested_tabs(), 'tab-3-1-1')
    assert content.tab_id == 'tab-3-1-1'
    assert content.blocks[1].elements[0].content == 'Hello\n'


def test_content_response_propagates_document_revision() -> None:
    content = project_tab_content(
        document([tab('tab-1', simple_body())]), 'tab-1'
    )
    assert content.document_id == 'document-1'
    assert content.revision_id == 'revision-1'


def test_reversed_requested_range_is_rejected() -> None:
    with pytest.raises(DocsInputError, match='range'):
        project_tab_content(
            document([tab('tab-1', simple_body())]),
            'tab-1',
            start_index=9,
            end_index=3,
        )


def test_tab_segment_is_index_aligned() -> None:
    body = document([tab('tab-1', simple_body())])
    body_segment = build_tab_segment(body, 'tab-1')
    assert body_segment.start_index == 0
    assert body_segment.end_index == 13
    assert body_segment.text.endswith('Hello\nWorld\n')
    assert len(body_segment.text.encode('utf-16-le')) // 2 == 13


def test_tab_segment_reserves_units_for_non_text_elements() -> None:
    marker = {
        'startIndex': 6,
        'endIndex': 7,
        'inlineObjectElement': {'inlineObjectId': 'object-1'},
    }
    closing = text_run(7, '\n')
    body = document(
        [tab('tab-1', [paragraph(1, 'Hello', tail=(marker, closing))])]
    )
    body_segment = build_tab_segment(body, 'tab-1')
    assert body_segment.start_index == 1
    assert body_segment.end_index == 8
    assert len(body_segment.text.encode('utf-16-le')) // 2 == 7


def test_tab_segment_preserves_surrogate_pairs() -> None:
    body = document([tab('tab-1', [paragraph(1, f'A{EMOJI}B\n')])])
    body_segment = build_tab_segment(body, 'tab-1')
    assert body_segment.text == f'A{EMOJI}B\n'
    assert body_segment.end_index - body_segment.start_index == 5


def test_tab_segment_for_unknown_tab_raises_not_found() -> None:
    with pytest.raises(DocsNotFoundError, match='Docs tab was not found'):
        build_tab_segment(document([tab('tab-1', simple_body())]), 'tab-9')


def test_deep_tab_search_is_rejected_on_read_path() -> None:
    payload = deep_tab_document(MAX_DOCS_TAB_DEPTH + 5)
    with pytest.raises(
        DocsUnsupportedError, match='nests tabs deeper than 10 levels'
    ):
        project_tab_content(payload, 'deep')


def test_deep_tab_search_is_rejected_when_building_segment() -> None:
    payload = deep_tab_document(MAX_DOCS_TAB_DEPTH + 5)
    with pytest.raises(
        DocsUnsupportedError, match='nests tabs deeper than 10 levels'
    ):
        build_tab_segment(payload, 'deep')


def test_tab_search_accepts_allowed_depth() -> None:
    payload = deep_tab_document(MAX_DOCS_TAB_DEPTH - 2)
    content = project_tab_content(payload, 'deep')
    assert content.tab_id == 'deep'


def test_deeply_nested_tables_are_rejected() -> None:
    payload = nested_table_document(MAX_DOCS_BLOCK_DEPTH + 5)
    with pytest.raises(
        DocsUnsupportedError,
        match='nests structural blocks deeper than 10 levels',
    ):
        project_tab_content(payload, 'tab-1')


def test_deeply_nested_tables_are_rejected_when_counting() -> None:
    payload = nested_table_document(MAX_DOCS_BLOCK_DEPTH + 5)
    with pytest.raises(
        DocsUnsupportedError,
        match='nests structural blocks deeper than 10 levels',
    ):
        count_paragraph_matches(payload, 'tab-1', 'Hi', True)


def test_shallow_nested_tables_are_accepted() -> None:
    payload = nested_table_document(2)
    content = project_tab_content(payload, 'tab-1')
    assert content.blocks[0].kind is DocsBlockKind.TABLE
    assert count_paragraph_matches(payload, 'tab-1', 'Hi', True) == 1


def test_zero_block_cap_projects_nothing() -> None:
    body = [paragraph(1, 'first\n'), paragraph(7, 'second\n')]
    content = project_tab_content(
        document([tab('tab-1', body)]), 'tab-1', max_blocks=0
    )
    assert content.blocks == ()
    assert content.truncated is True
    assert content.next_start_index == 1


def header_document(body_text: str, header_text: str) -> dict[str, Any]:
    """Build document header."""
    return {
        'documentId': 'document-1',
        'title': 'Doc',
        'revisionId': 'revision-1',
        'tabs': [
            {
                'tabProperties': {
                    'tabId': 'tab-1',
                    'title': 'Tab',
                    'index': 0,
                },
                'documentTab': {
                    'body': {'content': [paragraph(1, body_text)]},
                    'headers': {
                        'header-1': {'content': [paragraph(1, header_text)]}
                    },
                },
            }
        ],
    }


def test_replacement_count_includes_header_segments() -> None:
    payload = header_document('Alpha here\n', 'Alpha in header\n')
    assert count_paragraph_matches(payload, 'tab-1', 'Alpha', True) == 2


def test_replacement_count_covers_body_only_document() -> None:
    payload = header_document('Alpha here\n', 'nothing\n')
    assert count_paragraph_matches(payload, 'tab-1', 'Alpha', True) == 1


def test_wide_tab_tree_is_rejected_on_the_read_path() -> None:
    tabs = [
        {
            'tabProperties': {
                'tabId': f'tab-{index}',
                'title': f't{index}',
                'index': index,
            },
            'documentTab': {'body': {'content': [paragraph(1, 'x\n')]}},
        }
        for index in range(MAX_DOCS_TABS + 1)
    ]
    payload = {
        'documentId': 'document-1',
        'title': 'Doc',
        'revisionId': 'revision-1',
        'tabs': tabs,
    }
    with pytest.raises(DocsUnsupportedError, match='more than 200 tabs'):
        project_tab_content(payload, f'tab-{MAX_DOCS_TABS}')


def test_narrow_tab_tree_still_reads_on_the_read_path() -> None:
    tabs = [
        {
            'tabProperties': {
                'tabId': f'tab-{index}',
                'title': f't{index}',
                'index': index,
            },
            'documentTab': {'body': {'content': [paragraph(1, 'x\n')]}},
        }
        for index in range(MAX_DOCS_TABS)
    ]
    payload = {
        'documentId': 'document-1',
        'title': 'Doc',
        'revisionId': 'revision-1',
        'tabs': tabs,
    }
    content = project_tab_content(payload, f'tab-{MAX_DOCS_TABS - 1}')
    assert content.tab_id == f'tab-{MAX_DOCS_TABS - 1}'


def nested_tables_document() -> dict[str, Any]:
    """Build nested table document."""

    def cell(start: int, end: int, content: list[Any]) -> dict[str, Any]:
        """Build nested cell."""
        return {'startIndex': start, 'endIndex': end, 'content': content}

    def tbl(start: int, end: int, cells: list[Any]) -> dict[str, Any]:
        """Build nested table."""
        return {
            'startIndex': start,
            'endIndex': end,
            'table': {
                'rows': 1,
                'columns': len(cells),
                'tableRows': [
                    {
                        'startIndex': start + 1,
                        'endIndex': end - 1,
                        'tableCells': cells,
                    }
                ],
            },
        }

    inner = tbl(3, 12, [cell(4, 11, [paragraph(5, 'inner\n')])])
    outer = tbl(1, 20, [cell(2, 19, [inner, paragraph(13, 'after\n')])])
    return document([tab('tab-1', [outer])])


def test_whole_nested_table_can_be_deleted() -> None:
    segment = build_tab_segment(nested_tables_document(), 'tab-1')
    validate_delete_range(segment, 3, 12)


def test_range_across_two_nested_cells_is_refused() -> None:
    segment = build_tab_segment(nested_tables_document(), 'tab-1')
    with pytest.raises(DocsInputError, match='protected structural'):
        validate_delete_range(segment, 5, 14)


def test_paragraph_without_closing_newline_is_refused() -> None:
    marker = {
        'startIndex': 6,
        'endIndex': 7,
        'inlineObjectElement': {'inlineObjectId': 'object-1'},
    }
    payload = document([tab('tab-1', [paragraph(1, 'Hello', tail=(marker,))])])
    with pytest.raises(DocsProviderError):
        build_tab_segment(payload, 'tab-1')
