"""Parse Docs document structure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .constants import (
    MAX_DOCS_BLOCK_DEPTH,
    MAX_DOCS_BLOCKS,
    MAX_DOCS_ID_CHARS,
    MAX_DOCS_OUTPUT_CHARS,
    MAX_DOCS_TAB_DEPTH,
    MAX_DOCS_TABS,
    MAX_DOCS_TITLE_CHARS,
)
from .errors import DocsInputError, DocsNotFoundError, DocsProviderError
from .schemas import (
    DocsBlock,
    DocsBulletMarker,
    DocsContentResponse,
    DocsElementKind,
    DocsParagraphBlock,
    DocsSectionBreakBlock,
    DocsSegment,
    DocsTableBlock,
    DocsTableCell,
    DocsTableOfContentsBlock,
    DocsTableRow,
    DocsTabSummary,
    DocsTextElement,
    DocsUnsupportedBlock,
    DocumentSummary,
)

_PLACEHOLDER = '\ufffc'
_INVALID_RESPONSE = 'Docs response is invalid'
_TAB_NOT_FOUND = 'Docs tab was not found'

_ELEMENT_KINDS: dict[str, DocsElementKind] = {
    'textRun': DocsElementKind.TEXT_RUN,
    'inlineObjectElement': DocsElementKind.INLINE_OBJECT,
    'pageBreak': DocsElementKind.PAGE_BREAK,
    'columnBreak': DocsElementKind.COLUMN_BREAK,
    'footnoteReference': DocsElementKind.FOOTNOTE_REFERENCE,
    'horizontalRule': DocsElementKind.HORIZONTAL_RULE,
    'equation': DocsElementKind.EQUATION,
    'autoText': DocsElementKind.AUTO_TEXT,
    'person': DocsElementKind.PERSON,
    'richLink': DocsElementKind.RICH_LINK,
}


# UTF-16 primitives


def utf16_length(value: str) -> int:
    """Count UTF-16 code units."""
    return len(value.encode('utf-16-le')) // 2


def validate_utf16_boundary(value: str, index: int) -> None:
    """Validate UTF-16 boundary."""
    if index < 0:
        raise DocsInputError('Docs UTF-16 boundary is invalid')
    encoded = value.encode('utf-16-le')
    offset = index * 2
    if offset > len(encoded):
        raise DocsInputError('Docs UTF-16 boundary is invalid')
    try:
        encoded[:offset].decode('utf-16-le')
    except UnicodeDecodeError as error:
        raise DocsInputError('Docs UTF-16 boundary is invalid') from error


def validate_insert_index(segment: DocsSegment, index: int) -> None:
    """Validate insertion index position."""
    if index < segment.start_index or index > segment.end_index:
        raise DocsInputError('Docs index is outside the tab segment')
    validate_utf16_boundary(segment.text, index - segment.start_index)


def _validate_range_bounds(
    segment: DocsSegment,
    start_index: int,
    end_index: int,
) -> None:
    """Validate range against segment."""
    if start_index < segment.start_index or end_index > segment.end_index:
        raise DocsInputError('Docs range is outside the tab segment')
    if start_index >= end_index:
        raise DocsInputError('Docs range is invalid')


def _validate_range_boundaries(
    segment: DocsSegment,
    start_index: int,
    end_index: int,
) -> None:
    """Validate both range boundaries."""
    validate_utf16_boundary(segment.text, start_index - segment.start_index)
    validate_utf16_boundary(segment.text, end_index - segment.start_index)


def validate_content_range(
    segment: DocsSegment,
    start_index: int,
    end_index: int,
) -> None:
    """Validate addressable content range."""
    _validate_range_bounds(segment, start_index, end_index)
    _validate_range_boundaries(segment, start_index, end_index)


def validate_delete_range(
    segment: DocsSegment,
    start_index: int,
    end_index: int,
) -> None:
    """Validate deletable content range."""
    _validate_range_bounds(segment, start_index, end_index)
    if end_index > segment.end_index - 1:
        raise DocsInputError('Docs range includes the terminal newline')
    _validate_range_boundaries(segment, start_index, end_index)


# Provider shape guards


def _require_mapping(value: Any) -> Mapping[str, Any]:
    """Require provider mapping value."""
    if not isinstance(value, Mapping):
        raise DocsProviderError(_INVALID_RESPONSE)
    return value


def _require_sequence(value: Any) -> Sequence[Any]:
    """Require provider sequence value."""
    if not isinstance(value, list | tuple):
        raise DocsProviderError(_INVALID_RESPONSE)
    return value


def _require_identifier(value: Any, limit: int = MAX_DOCS_ID_CHARS) -> str:
    """Require bounded provider identifier."""
    if not isinstance(value, str) or not value or len(value) > limit:
        raise DocsProviderError(_INVALID_RESPONSE)
    return value


def _optional_text(
    value: Any, limit: int = MAX_DOCS_TITLE_CHARS
) -> str | None:
    """Return bounded optional text."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise DocsProviderError(_INVALID_RESPONSE)
    return value


def _require_integer(value: Any, default: int | None = None) -> int:
    """Require provider integer value."""
    if value is None and default is not None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise DocsProviderError(_INVALID_RESPONSE)
    return value


def _element_bounds(element: Mapping[str, Any]) -> tuple[int, int]:
    """Return validated element bounds."""
    start = _require_integer(element.get('startIndex'), 0)
    end = _require_integer(element.get('endIndex'))
    if start < 0 or end < start:
        raise DocsProviderError(_INVALID_RESPONSE)
    return start, end


# Tab tree


def _tab_body_content(node: Mapping[str, Any]) -> Sequence[Any]:
    """Return provider tab content."""
    document_tab = node.get('documentTab')
    if document_tab is None:
        return ()
    body = _require_mapping(document_tab).get('body')
    if body is None:
        return ()
    content = _require_mapping(body).get('content')
    if content is None:
        return ()
    return _require_sequence(content)


def _tab_bounds(node: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Return provider tab bounds."""
    content = _tab_body_content(node)
    if not content:
        return None, None
    start, _ = _element_bounds(_require_mapping(content[0]))
    _, end = _element_bounds(_require_mapping(content[-1]))
    return start, end


def _parse_tab(
    node: Mapping[str, Any],
    depth: int,
    counter: list[int],
) -> DocsTabSummary:
    """Parse single provider tab."""
    if depth > MAX_DOCS_TAB_DEPTH:
        raise DocsProviderError(_INVALID_RESPONSE)
    counter[0] += 1
    if counter[0] > MAX_DOCS_TABS:
        raise DocsProviderError(_INVALID_RESPONSE)
    properties = _require_mapping(node.get('tabProperties'))
    raw_children = node.get('childTabs') or ()
    children = tuple(
        _parse_tab(_require_mapping(child), depth + 1, counter)
        for child in _require_sequence(raw_children)
    )
    start, end = _tab_bounds(node)
    parent = properties.get('parentTabId')
    return DocsTabSummary(
        tab_id=_require_identifier(properties.get('tabId')),
        title=_optional_text(properties.get('title')),
        index=_require_integer(properties.get('index'), 0),
        parent_tab_id=(
            None if parent is None else _require_identifier(parent)
        ),
        nesting_level=_require_integer(properties.get('nestingLevel'), 0),
        child_count=len(children),
        start_index=start,
        end_index=end,
        children=children,
    )


def _document_tabs(document: Mapping[str, Any]) -> Sequence[Any]:
    """Return provider root tabs."""
    tabs = _require_sequence(_require_mapping(document).get('tabs'))
    if not tabs:
        raise DocsProviderError(_INVALID_RESPONSE)
    return tabs


def parse_document_tabs(document: Mapping[str, Any]) -> DocumentSummary:
    """Parse recursive document tabs."""
    payload = _require_mapping(document)
    tabs = _document_tabs(payload)
    counter = [0]
    parsed = tuple(
        _parse_tab(_require_mapping(node), 1, counter) for node in tabs
    )
    title = _optional_text(payload.get('title'))
    return DocumentSummary(
        document_id=_require_identifier(payload.get('documentId')),
        title=title if title is not None else '',
        revision_id=_require_identifier(payload.get('revisionId')),
        tabs=parsed,
    )


def _find_tab_node(
    nodes: Sequence[Any],
    tab_id: str,
    depth: int = 1,
) -> Mapping[str, Any] | None:
    """Find provider tab node."""
    if depth > MAX_DOCS_TAB_DEPTH:
        raise DocsProviderError(_INVALID_RESPONSE)
    for raw in nodes:
        node = _require_mapping(raw)
        properties = _require_mapping(node.get('tabProperties'))
        if properties.get('tabId') == tab_id:
            return node
        children = node.get('childTabs') or ()
        found = _find_tab_node(_require_sequence(children), tab_id, depth + 1)
        if found is not None:
            return found
    return None


def _require_tab_node(
    document: Mapping[str, Any],
    tab_id: str,
) -> Mapping[str, Any]:
    """Require named provider tab."""
    node = _find_tab_node(_document_tabs(document), tab_id)
    if node is None:
        raise DocsNotFoundError(_TAB_NOT_FOUND)
    return node


# Segment reconstruction


def _element_kind(element: Mapping[str, Any]) -> DocsElementKind:
    """Detect paragraph element kind."""
    for key, kind in _ELEMENT_KINDS.items():
        if key in element:
            return kind
    return DocsElementKind.UNKNOWN


def _element_text(element: Mapping[str, Any]) -> str:
    """Return aligned element text."""
    start, end = _element_bounds(element)
    if _element_kind(element) is DocsElementKind.TEXT_RUN:
        run = _require_mapping(element['textRun'])
        content = run.get('content')
        if not isinstance(content, str):
            raise DocsProviderError(_INVALID_RESPONSE)
        return content
    return _PLACEHOLDER * (end - start)


def _structural_text(element: Mapping[str, Any]) -> str:
    """Return aligned block text."""
    start, end = _element_bounds(element)
    if 'paragraph' not in element:
        return _PLACEHOLDER * (end - start)
    paragraph = _require_mapping(element['paragraph'])
    elements = _require_sequence(paragraph.get('elements') or ())
    return ''.join(_element_text(_require_mapping(item)) for item in elements)


def build_tab_segment(
    document: Mapping[str, Any],
    tab_id: str,
) -> DocsSegment:
    """Build aligned tab segment."""
    node = _require_tab_node(document, tab_id)
    content = _tab_body_content(node)
    if not content:
        return DocsSegment(tab_id=tab_id, start_index=0, end_index=0, text='')
    start, _ = _element_bounds(_require_mapping(content[0]))
    _, end = _element_bounds(_require_mapping(content[-1]))
    text = ''.join(
        _structural_text(_require_mapping(item)) for item in content
    )
    if utf16_length(text) != end - start:
        raise DocsProviderError(_INVALID_RESPONSE)
    return DocsSegment(
        tab_id=tab_id, start_index=start, end_index=end, text=text
    )


# Typed block projection


def _project_element(
    element: Mapping[str, Any],
    unsupported: set[str],
) -> tuple[DocsTextElement, int]:
    """Project single paragraph element."""
    start, end = _element_bounds(element)
    kind = _element_kind(element)
    if kind is DocsElementKind.TEXT_RUN:
        run = _require_mapping(element['textRun'])
        content = run.get('content')
        if not isinstance(content, str):
            raise DocsProviderError(_INVALID_RESPONSE)
        projected = DocsTextElement(
            kind=kind, start_index=start, end_index=end, content=content
        )
        return projected, len(content)
    unsupported.add(kind.value)
    projected = DocsTextElement(kind=kind, start_index=start, end_index=end)
    return projected, 0


def _project_paragraph(
    element: Mapping[str, Any],
    unsupported: set[str],
) -> tuple[DocsParagraphBlock, int]:
    """Project single paragraph block."""
    start, end = _element_bounds(element)
    paragraph = _require_mapping(element['paragraph'])
    style = paragraph.get('paragraphStyle')
    named_style = (
        _optional_text(_require_mapping(style).get('namedStyleType'))
        if style is not None
        else None
    )
    bullet_payload = paragraph.get('bullet')
    bullet = None
    if bullet_payload is not None:
        marker = _require_mapping(bullet_payload)
        list_id = marker.get('listId')
        bullet = DocsBulletMarker(
            list_id=None if list_id is None else _require_identifier(list_id),
            nesting_level=_require_integer(marker.get('nestingLevel'), 0),
        )
    elements: list[DocsTextElement] = []
    characters = 0
    for item in _require_sequence(paragraph.get('elements') or ()):
        projected, size = _project_element(_require_mapping(item), unsupported)
        elements.append(projected)
        characters += size
    block = DocsParagraphBlock(
        start_index=start,
        end_index=end,
        named_style=named_style,
        bullet=bullet,
        elements=tuple(elements),
    )
    return block, characters


def _project_table(
    element: Mapping[str, Any],
    unsupported: set[str],
    depth: int,
) -> tuple[DocsTableBlock, int]:
    """Project single table block."""
    start, end = _element_bounds(element)
    payload = _require_mapping(element['table'])
    characters = 0
    rows: list[DocsTableRow] = []
    for raw_row in _require_sequence(payload.get('tableRows') or ()):
        row = _require_mapping(raw_row)
        row_start, row_end = _element_bounds(row)
        cells: list[DocsTableCell] = []
        for raw_cell in _require_sequence(row.get('tableCells') or ()):
            cell = _require_mapping(raw_cell)
            cell_start, cell_end = _element_bounds(cell)
            blocks: list[DocsBlock] = []
            for raw_block in _require_sequence(cell.get('content') or ()):
                projected, size = _project_block(
                    _require_mapping(raw_block), unsupported, depth + 1
                )
                blocks.append(projected)
                characters += size
            cells.append(
                DocsTableCell(
                    start_index=cell_start,
                    end_index=cell_end,
                    blocks=tuple(blocks),
                )
            )
        rows.append(
            DocsTableRow(
                start_index=row_start,
                end_index=row_end,
                cells=tuple(cells),
            )
        )
    block = DocsTableBlock(
        start_index=start,
        end_index=end,
        row_count=_require_integer(payload.get('rows'), len(rows)),
        column_count=_require_integer(payload.get('columns'), 0),
        rows=tuple(rows),
    )
    return block, characters


def _project_block(
    element: Mapping[str, Any],
    unsupported: set[str],
    depth: int = 1,
) -> tuple[DocsBlock, int]:
    """Project single structural block."""
    if depth > MAX_DOCS_BLOCK_DEPTH:
        raise DocsProviderError(_INVALID_RESPONSE)
    start, end = _element_bounds(element)
    if 'paragraph' in element:
        return _project_paragraph(element, unsupported)
    if 'table' in element:
        return _project_table(element, unsupported, depth)
    if 'sectionBreak' in element:
        return DocsSectionBreakBlock(start_index=start, end_index=end), 0
    if 'tableOfContents' in element:
        return DocsTableOfContentsBlock(start_index=start, end_index=end), 0
    unsupported.add(DocsElementKind.UNKNOWN.value)
    return (
        DocsUnsupportedBlock(
            start_index=start,
            end_index=end,
            unsupported_kind=DocsElementKind.UNKNOWN.value,
        ),
        0,
    )


def _selected_elements(
    content: Sequence[Any],
    start_index: int | None,
    end_index: int | None,
) -> list[Mapping[str, Any]]:
    """Select elements overlapping range."""
    selected: list[Mapping[str, Any]] = []
    for raw in content:
        element = _require_mapping(raw)
        start, end = _element_bounds(element)
        if end_index is not None and start >= end_index:
            continue
        if start_index is not None and end <= start_index:
            continue
        selected.append(element)
    return selected


def project_tab_content(
    document: Mapping[str, Any],
    tab_id: str,
    start_index: int | None = None,
    end_index: int | None = None,
    max_blocks: int = MAX_DOCS_BLOCKS,
    max_chars: int = MAX_DOCS_OUTPUT_CHARS,
) -> DocsContentResponse:
    """Project bounded tab content."""
    if (
        start_index is not None
        and end_index is not None
        and start_index >= end_index
    ):
        raise DocsInputError('Docs range is invalid')
    if start_index is not None and start_index < 0:
        raise DocsInputError('Docs range is invalid')
    payload = _require_mapping(document)
    node = _require_tab_node(payload, tab_id)
    content = _tab_body_content(node)
    selected = _selected_elements(content, start_index, end_index)
    unsupported: set[str] = set()
    blocks: list[DocsBlock] = []
    characters = 0
    truncated = False
    next_start: int | None = None
    for element in selected:
        element_start, _ = _element_bounds(element)
        if blocks and len(blocks) >= max_blocks:
            truncated = True
            next_start = element_start
            break
        projected, size = _project_block(element, unsupported)
        if blocks and characters + size > max_chars:
            truncated = True
            next_start = element_start
            break
        blocks.append(projected)
        characters += size
    bounds_start = (
        blocks[0].start_index
        if blocks
        else (start_index if start_index is not None else 0)
    )
    bounds_end = (
        blocks[-1].end_index
        if blocks
        else (end_index if end_index is not None else 0)
    )
    return DocsContentResponse(
        document_id=_require_identifier(payload.get('documentId')),
        revision_id=_require_identifier(payload.get('revisionId')),
        tab_id=tab_id,
        start_index=bounds_start,
        end_index=bounds_end,
        blocks=tuple(blocks),
        text_characters=characters,
        truncated=truncated,
        next_start_index=next_start,
        unsupported_kinds=tuple(sorted(unsupported)),
    )


# Replacement preflight


def _searchable_segments(
    content: Sequence[Any],
    depth: int = 1,
) -> list[str]:
    """Collect contiguous searchable text."""
    if depth > MAX_DOCS_BLOCK_DEPTH:
        raise DocsProviderError(_INVALID_RESPONSE)
    segments: list[str] = []
    for raw in content:
        element = _require_mapping(raw)
        if 'paragraph' in element:
            paragraph = _require_mapping(element['paragraph'])
            current: list[str] = []
            for raw_item in _require_sequence(paragraph.get('elements') or ()):
                item = _require_mapping(raw_item)
                if _element_kind(item) is DocsElementKind.TEXT_RUN:
                    run = _require_mapping(item['textRun'])
                    value = run.get('content')
                    if not isinstance(value, str):
                        raise DocsProviderError(_INVALID_RESPONSE)
                    current.append(value)
                elif current:
                    segments.append(''.join(current))
                    current = []
            if current:
                segments.append(''.join(current))
        elif 'table' in element:
            payload = _require_mapping(element['table'])
            for raw_row in _require_sequence(payload.get('tableRows') or ()):
                row = _require_mapping(raw_row)
                cells = _require_sequence(row.get('tableCells') or ())
                for raw_cell in cells:
                    cell = _require_mapping(raw_cell)
                    segments.extend(
                        _searchable_segments(
                            _require_sequence(cell.get('content') or ()),
                            depth + 1,
                        )
                    )
    return segments


def count_paragraph_matches(
    document: Mapping[str, Any],
    tab_id: str,
    literal: str,
    match_case: bool,
) -> int:
    """Count matches in paragraphs."""
    node = _require_tab_node(document, tab_id)
    needle = literal if match_case else literal.casefold()
    total = 0
    for segment_text in _searchable_segments(_tab_body_content(node)):
        haystack = segment_text if match_case else segment_text.casefold()
        total += haystack.count(needle)
    return total
