"""Define Docs service schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class DocsModel(BaseModel):
    """Configure Docs schema model."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class DocsBlockKind(StrEnum):
    """Select structural block kind."""

    PARAGRAPH = 'paragraph'
    TABLE = 'table'
    SECTION_BREAK = 'section_break'
    TABLE_OF_CONTENTS = 'table_of_contents'
    UNSUPPORTED = 'unsupported'


class DocsElementKind(StrEnum):
    """Select paragraph element kind."""

    TEXT_RUN = 'text_run'
    INLINE_OBJECT = 'inline_object'
    PAGE_BREAK = 'page_break'
    COLUMN_BREAK = 'column_break'
    FOOTNOTE_REFERENCE = 'footnote_reference'
    HORIZONTAL_RULE = 'horizontal_rule'
    EQUATION = 'equation'
    AUTO_TEXT = 'auto_text'
    PERSON = 'person'
    RICH_LINK = 'rich_link'
    UNKNOWN = 'unknown'


class DocsSegment(DocsModel):
    """Describe indexed tab segment."""

    tab_id: str
    start_index: int
    end_index: int
    text: str


class DocsBulletMarker(DocsModel):
    """Describe paragraph bullet marker."""

    list_id: str | None = None
    nesting_level: int = 0


class DocsTextElement(DocsModel):
    """Describe single paragraph element."""

    kind: DocsElementKind
    start_index: int
    end_index: int
    content: str | None = None


class DocsParagraphBlock(DocsModel):
    """Describe projected paragraph block."""

    kind: Literal[DocsBlockKind.PARAGRAPH] = DocsBlockKind.PARAGRAPH
    start_index: int
    end_index: int
    named_style: str | None = None
    bullet: DocsBulletMarker | None = None
    elements: tuple[DocsTextElement, ...] = ()


class DocsSectionBreakBlock(DocsModel):
    """Describe projected section break."""

    kind: Literal[DocsBlockKind.SECTION_BREAK] = DocsBlockKind.SECTION_BREAK
    start_index: int
    end_index: int


class DocsTableOfContentsBlock(DocsModel):
    """Describe projected contents table."""

    kind: Literal[DocsBlockKind.TABLE_OF_CONTENTS] = (
        DocsBlockKind.TABLE_OF_CONTENTS
    )
    start_index: int
    end_index: int


class DocsUnsupportedBlock(DocsModel):
    """Describe unsupported structural block."""

    kind: Literal[DocsBlockKind.UNSUPPORTED] = DocsBlockKind.UNSUPPORTED
    start_index: int
    end_index: int
    unsupported_kind: str


class DocsTableCell(DocsModel):
    """Describe projected table cell."""

    start_index: int
    end_index: int
    blocks: tuple[DocsBlock, ...] = ()


class DocsTableRow(DocsModel):
    """Describe projected table row."""

    start_index: int
    end_index: int
    cells: tuple[DocsTableCell, ...] = ()


class DocsTableBlock(DocsModel):
    """Describe projected table block."""

    kind: Literal[DocsBlockKind.TABLE] = DocsBlockKind.TABLE
    start_index: int
    end_index: int
    row_count: int
    column_count: int
    rows: tuple[DocsTableRow, ...] = ()


DocsBlock = Annotated[
    DocsParagraphBlock
    | DocsTableBlock
    | DocsSectionBreakBlock
    | DocsTableOfContentsBlock
    | DocsUnsupportedBlock,
    Field(discriminator='kind'),
]


class DocsTabSummary(DocsModel):
    """Describe single document tab."""

    tab_id: str
    title: str | None = None
    index: int
    parent_tab_id: str | None = None
    nesting_level: int = 0
    child_count: int = 0
    start_index: int | None = None
    end_index: int | None = None
    children: tuple[DocsTabSummary, ...] = ()


class DocumentSummary(DocsModel):
    """Describe document tab metadata."""

    document_id: str
    title: str
    revision_id: str
    tabs: tuple[DocsTabSummary, ...]


class DocsContentResponse(DocsModel):
    """Return bounded tab content."""

    document_id: str
    revision_id: str
    tab_id: str
    start_index: int
    end_index: int
    blocks: tuple[DocsBlock, ...] = ()
    text_characters: int = 0
    truncated: bool = False
    next_start_index: int | None = None
    unsupported_kinds: tuple[str, ...] = ()


class DocsBatchOperationType(StrEnum):
    """Select Docs batch operation."""

    INSERT_TEXT = 'insert_text'
    DELETE_RANGE = 'delete_range'
    REPLACE_TEXT = 'replace_text'
    INSERT_PAGE_BREAK = 'insert_page_break'
    UPDATE_TEXT_STYLE = 'update_text_style'
    UPDATE_PARAGRAPH_STYLE = 'update_paragraph_style'
    CREATE_BULLETS = 'create_bullets'
    DELETE_BULLETS = 'delete_bullets'


class DocsNamedStyle(StrEnum):
    """Select allowed paragraph style."""

    TITLE = 'title'
    SUBTITLE = 'subtitle'
    NORMAL_TEXT = 'normal_text'
    HEADING_1 = 'heading_1'
    HEADING_2 = 'heading_2'
    HEADING_3 = 'heading_3'
    HEADING_4 = 'heading_4'
    HEADING_5 = 'heading_5'
    HEADING_6 = 'heading_6'


class DocsAlignment(StrEnum):
    """Select allowed paragraph alignment."""

    START = 'start'
    CENTER = 'center'
    END = 'end'
    JUSTIFIED = 'justified'


class DocsBulletPreset(StrEnum):
    """Select allowed bullet preset."""

    DISC_CIRCLE_SQUARE = 'disc_circle_square'
    ARROW_DIAMOND_DISC = 'arrow_diamond_disc'
    CHECKBOX = 'checkbox'
    DECIMAL_ALPHA_ROMAN = 'decimal_alpha_roman'
    DECIMAL_NESTED = 'decimal_nested'


class DocsInsertTextOperation(DocsModel):
    """Insert text batch operation."""

    operation: Literal[DocsBatchOperationType.INSERT_TEXT]
    index: int
    text: str


class DocsDeleteRangeOperation(DocsModel):
    """Delete range batch operation."""

    operation: Literal[DocsBatchOperationType.DELETE_RANGE]
    start_index: int
    end_index: int


class DocsReplaceTextOperation(DocsModel):
    """Replace text batch operation."""

    operation: Literal[DocsBatchOperationType.REPLACE_TEXT]
    search_text: str
    replacement_text: str
    match_case: bool
    expected_occurrences: int


class DocsInsertPageBreakOperation(DocsModel):
    """Insert page break operation."""

    operation: Literal[DocsBatchOperationType.INSERT_PAGE_BREAK]
    index: int


class DocsUpdateTextStyleOperation(DocsModel):
    """Update text style operation."""

    operation: Literal[DocsBatchOperationType.UPDATE_TEXT_STYLE]
    start_index: int
    end_index: int
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None


class DocsUpdateParagraphStyleOperation(DocsModel):
    """Update paragraph style operation."""

    operation: Literal[DocsBatchOperationType.UPDATE_PARAGRAPH_STYLE]
    start_index: int
    end_index: int
    named_style: DocsNamedStyle | None = None
    alignment: DocsAlignment | None = None


class DocsCreateBulletsOperation(DocsModel):
    """Create paragraph bullets operation."""

    operation: Literal[DocsBatchOperationType.CREATE_BULLETS]
    start_index: int
    end_index: int
    preset: DocsBulletPreset


class DocsDeleteBulletsOperation(DocsModel):
    """Delete paragraph bullets operation."""

    operation: Literal[DocsBatchOperationType.DELETE_BULLETS]
    start_index: int
    end_index: int


DocsBatchOperation = Annotated[
    DocsInsertTextOperation
    | DocsDeleteRangeOperation
    | DocsReplaceTextOperation
    | DocsInsertPageBreakOperation
    | DocsUpdateTextStyleOperation
    | DocsUpdateParagraphStyleOperation
    | DocsCreateBulletsOperation
    | DocsDeleteBulletsOperation,
    Field(discriminator='operation'),
]


class DocsBatchReply(DocsModel):
    """Describe normalized batch reply."""

    operation: DocsBatchOperationType
    occurrences_changed: int | None = None


class DocsBatchResult(DocsModel):
    """Describe applied atomic batch."""

    document_id: str
    tab_id: str
    operation_count: int
    required_revision_id: str
    replies: tuple[DocsBatchReply, ...] = ()


class DocsMutationResult(DocsModel):
    """Describe applied document mutation."""

    document_id: str
    tab_id: str
    required_revision_id: str


class DocsCreateResult(DocsModel):
    """Describe created document root."""

    document_id: str
    title: str
    tab_id: str
    required_revision_id: str


class DocsReplaceResult(DocsModel):
    """Describe applied text replacement."""

    document_id: str
    tab_id: str
    occurrences_changed: int
    required_revision_id: str


DocsTableCell.model_rebuild()
DocsTableRow.model_rebuild()
DocsTableBlock.model_rebuild()
DocsTabSummary.model_rebuild()
DocsContentResponse.model_rebuild()
