"""Validate Sheets A1 ranges."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .constants import (
    MAX_SHEETS_A1_CHARS,
    MAX_SHEETS_TITLE_CHARS,
)
from .errors import SheetsInputError

_FORBIDDEN_SHEET_CHARS = frozenset('[]*?:/\\')
_CELL_RE = re.compile(r'^\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}$')


def _validate_sheet_component(sheet: str) -> None:
    """Validate sheet title component."""
    if not sheet or len(sheet) > MAX_SHEETS_TITLE_CHARS:
        raise SheetsInputError('Sheets range is invalid')
    if sheet.startswith("'") or sheet.endswith("'"):
        if (
            not (sheet.startswith("'") and sheet.endswith("'"))
            or len(sheet) < 2
        ):
            raise SheetsInputError('Sheets range is invalid')
        inner = sheet[1:-1]
        if not inner or len(inner) > MAX_SHEETS_TITLE_CHARS:
            raise SheetsInputError('Sheets range is invalid')
        if inner.replace("''", '').count("'") > 0:
            raise SheetsInputError('Sheets range is invalid')
        if any(character in _FORBIDDEN_SHEET_CHARS for character in inner):
            raise SheetsInputError('Sheets range is invalid')
    else:
        if "'" in sheet or len(sheet) > MAX_SHEETS_TITLE_CHARS:
            raise SheetsInputError('Sheets range is invalid')
        if any(character.isspace() for character in sheet):
            raise SheetsInputError('Sheets range is invalid')
        if any(character in _FORBIDDEN_SHEET_CHARS for character in sheet):
            raise SheetsInputError('Sheets range is invalid')


def _validate_cell_component(cells: str) -> None:
    """Validate cell range component."""
    if not cells:
        raise SheetsInputError('Sheets range is invalid')
    colon_count = cells.count(':')
    if colon_count == 0:
        if not _CELL_RE.match(cells):
            raise SheetsInputError('Sheets range is invalid')
    elif colon_count == 1:
        start, end = cells.split(':')
        if not _CELL_RE.match(start) or not _CELL_RE.match(end):
            raise SheetsInputError('Sheets range is invalid')
    else:
        raise SheetsInputError('Sheets range is invalid')


def validate_a1_range(value: str) -> str:
    """Validate qualified A1 range."""
    if not value or len(value) > MAX_SHEETS_A1_CHARS:
        raise SheetsInputError('Sheets range is invalid')
    if any(ord(character) < 32 for character in value):
        raise SheetsInputError('Sheets range is invalid')
    sheet, separator, cells = value.rpartition('!')
    if not separator or not sheet or not cells:
        raise SheetsInputError('Sheets range must be sheet qualified')
    _validate_sheet_component(sheet)
    _validate_cell_component(cells)
    return value


def count_cells(values: Sequence[Sequence[object]]) -> int:
    """Count total sheet cells."""
    return sum(len(row) for row in values)
