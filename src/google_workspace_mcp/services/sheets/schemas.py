"""Define Sheets service schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SheetsModel(BaseModel):
    """Configure Sheets schema model."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class SheetsRenderMode(StrEnum):
    """Select value render mode."""

    FORMATTED = 'formatted'
    UNFORMATTED = 'unformatted'
    FORMULA = 'formula'


class SheetsDateTimeMode(StrEnum):
    """Select date render mode."""

    SERIAL_NUMBER = 'serial_number'
    FORMATTED_STRING = 'formatted_string'


class SheetsInputMode(StrEnum):
    """Select value input mode."""

    RAW = 'raw'
    USER_ENTERED = 'user_entered'


class SheetsInsertMode(StrEnum):
    """Select row insert mode."""

    INSERT_ROWS = 'insert_rows'
    OVERWRITE = 'overwrite'


class MajorDimension(StrEnum):
    """Select grid major dimension."""

    ROWS = 'rows'
    COLUMNS = 'columns'


class SheetSummary(SheetsModel):
    """Describe spreadsheet sheet."""

    sheet_id: int
    title: str
    index: int
    sheet_type: str
    row_count: int | None = None
    column_count: int | None = None


class SpreadsheetSummary(SheetsModel):
    """Describe spreadsheet metadata."""

    spreadsheet_id: str
    title: str
    locale: str | None = None
    time_zone: str | None = None
    url: str | None = None
    sheets: tuple[SheetSummary, ...]


class SheetsValueRange(SheetsModel):
    """Describe spreadsheet values."""

    requested_range: str
    resolved_range: str
    major_dimension: MajorDimension
    values: tuple[tuple[object, ...], ...]
    row_count: int
    column_count: int
    cell_count: int


class SheetsBatchReadResult(SheetsModel):
    """Return spreadsheet ranges."""

    spreadsheet_id: str
    ranges: tuple[SheetsValueRange, ...]


class SheetsWriteRange(SheetsModel):
    """Describe range write payload."""

    range_name: str
    values: tuple[tuple[object, ...], ...]


class SheetsWriteResult(SheetsModel):
    """Describe spreadsheet write result."""

    spreadsheet_id: str
    updated_range: str
    updated_rows: int
    updated_columns: int
    updated_cells: int


class SheetsAppendResult(SheetsModel):
    """Describe spreadsheet append result."""

    spreadsheet_id: str
    table_range: str | None = None
    updated_range: str
    updated_rows: int
    updated_columns: int
    updated_cells: int


class SheetsBatchWriteResult(SheetsModel):
    """Describe batch write result."""

    spreadsheet_id: str
    total_updated_rows: int
    total_updated_columns: int
    total_updated_cells: int
    total_updated_sheets: int
    responses: tuple[SheetsWriteResult, ...]


class SheetsClearResult(SheetsModel):
    """Describe spreadsheet clear result."""

    spreadsheet_id: str
    cleared_ranges: tuple[str, ...]


class SpreadsheetCreateResult(SheetsModel):
    """Describe spreadsheet creation result."""

    spreadsheet_id: str
    title: str
    locale: str | None = None
    time_zone: str | None = None
    url: str | None = None
    sheets: tuple[SheetSummary, ...]


class SheetMutationResult(SheetsModel):
    """Describe sheet mutation result."""

    spreadsheet_id: str
    sheet_id: int
    title: str
    index: int
    sheet_type: str
    row_count: int | None = None
    column_count: int | None = None


class SheetCopyResult(SheetsModel):
    """Describe sheet copy result."""

    source_spreadsheet_id: str
    destination_spreadsheet_id: str
    sheet_id: int
    title: str
    index: int
    sheet_type: str
    row_count: int | None = None
    column_count: int | None = None
