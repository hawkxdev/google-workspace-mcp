"""Test Sheets A1 semantics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from google_workspace_mcp.services.sheets.a1 import (
    count_cells,
    validate_a1_range,
)
from google_workspace_mcp.services.sheets.constants import (
    MAX_SHEETS_A1_CHARS,
    MAX_SHEETS_CELLS,
    MAX_SHEETS_PAYLOAD_BYTES,
    MAX_SHEETS_RANGES,
    MAX_SHEETS_TEXT_CHARS,
    MAX_SHEETS_TITLE_CHARS,
    REQUEST_RETRIES,
    SHEETS_SCOPES,
)
from google_workspace_mcp.services.sheets.errors import (
    SheetsError,
    SheetsInputError,
    SheetsNotFoundError,
    SheetsProviderError,
    SheetsRateLimitError,
    SheetsScopeError,
)
from google_workspace_mcp.services.sheets.schemas import (
    MajorDimension,
    SheetsDateTimeMode,
    SheetsInputMode,
    SheetsInsertMode,
    SheetsModel,
    SheetsRenderMode,
)


def test_a1_requires_explicit_sheet() -> None:
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        validate_a1_range('A1:B2')


def test_a1_accepts_quoted_sheet_and_doubled_quote() -> None:
    assert validate_a1_range("'John''s Data'!A1:C5") == (
        "'John''s Data'!A1:C5"
    )


def test_a1_rejects_control_character() -> None:
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range("'Sheet1'!A1:\x00B2")


def test_input_mode_has_no_implicit_default() -> None:
    assert {item.value for item in SheetsInputMode} == {
        'raw',
        'user_entered',
    }


def test_a1_accepts_valid_unquoted_sheet_and_ranges() -> None:
    assert validate_a1_range('Sheet1!A1:B2') == 'Sheet1!A1:B2'
    assert validate_a1_range('Sheet1!A1') == 'Sheet1!A1'
    assert validate_a1_range('Sheet1!$A$1:$B$10') == 'Sheet1!$A$1:$B$10'
    assert validate_a1_range('Sheet1!A1:$B$10') == 'Sheet1!A1:$B$10'


def test_a1_rejects_whole_column_and_whole_row_ranges() -> None:
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!A:B')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!A:C')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!1:10')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!A1:B')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!A:B10')


def test_a1_rejects_empty_and_excessive_length() -> None:
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!' + ('A' * (MAX_SHEETS_A1_CHARS + 1)))


def test_a1_rejects_missing_separator_and_empty_sheet() -> None:
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        validate_a1_range('Sheet1')
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        validate_a1_range("'Sheet1'")
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        validate_a1_range('!A1:B2')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range("''!A1:B2")


def test_a1_rejects_unclosed_and_unescaped_quotes() -> None:
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range("'Sheet1!A1:B2")
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range("Sheet1'!A1:B2")
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range("'John's Data'!A1:B2")


def test_a1_rejects_named_ranges_and_empty_cells() -> None:
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range('Sheet1!NamedRange')
    with pytest.raises(SheetsInputError, match='invalid'):
        validate_a1_range("'Sheet1'!MyNamedRange")
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        validate_a1_range('Sheet1!')


def test_count_cells_calculates_total_cells() -> None:
    assert count_cells(((1, 2), (3, 4, 5))) == 5
    assert count_cells(()) == 0
    assert count_cells(((), ())) == 0


def test_constants_definitions() -> None:
    assert len(SHEETS_SCOPES) == 2
    assert 'https://www.googleapis.com/auth/spreadsheets' in SHEETS_SCOPES
    assert 'https://www.googleapis.com/auth/drive.file' in SHEETS_SCOPES
    assert MAX_SHEETS_RANGES == 20
    assert MAX_SHEETS_CELLS == 10_000
    assert MAX_SHEETS_PAYLOAD_BYTES == 1_048_576
    assert MAX_SHEETS_A1_CHARS == 512
    assert MAX_SHEETS_TITLE_CHARS == 100
    assert MAX_SHEETS_TEXT_CHARS == 50_000
    assert REQUEST_RETRIES == 3


def test_error_hierarchy() -> None:
    assert issubclass(SheetsInputError, SheetsError)
    assert issubclass(SheetsNotFoundError, SheetsError)
    assert issubclass(SheetsProviderError, SheetsError)
    assert issubclass(SheetsRateLimitError, SheetsError)
    assert issubclass(SheetsScopeError, SheetsError)


def test_enum_values() -> None:
    assert {item.value for item in SheetsRenderMode} == {
        'formatted',
        'unformatted',
        'formula',
    }
    assert {item.value for item in SheetsDateTimeMode} == {
        'serial_number',
        'formatted_string',
    }
    assert {item.value for item in SheetsInsertMode} == {
        'insert_rows',
        'overwrite',
    }
    assert {item.value for item in MajorDimension} == {
        'rows',
        'columns',
    }


def test_sheets_model_forbids_extra_and_is_frozen() -> None:
    class DummyModel(SheetsModel):
        """Provide dummy test model."""

        title: str

    instance = DummyModel(title='test')
    assert instance.title == 'test'
    with pytest.raises(ValidationError):
        DummyModel(title='test', extra_field='invalid')  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        instance.title = 'modified'  # type: ignore[misc]
