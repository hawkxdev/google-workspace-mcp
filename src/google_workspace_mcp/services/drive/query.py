"""Build Drive search queries."""

from __future__ import annotations

import re
from datetime import datetime

from .errors import DriveInputError
from .schemas import DriveSearchFilters

_RFC3339_PATTERN = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
    r'(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$'
)


def escape_drive_literal(value: str) -> str:
    """Escape Drive literal string."""
    return value.replace('\\', '\\\\').replace("'", "\\'")


def _validate_rfc3339_timestamp(value: str) -> datetime:
    """Validate RFC3339 aware timestamp."""
    if _RFC3339_PATTERN.fullmatch(value) is None:
        raise DriveInputError('timestamp must be valid RFC3339')
    normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise DriveInputError('timestamp must be valid RFC3339') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DriveInputError('timestamp must be offset-aware')
    return parsed


def build_drive_query(
    filters: DriveSearchFilters,
    *,
    folder_id: str | None = None,
) -> str:
    """Build Drive search query."""
    if (
        folder_id is not None
        and filters.parent_id is not None
        and folder_id != filters.parent_id
    ):
        raise DriveInputError('conflicting folder and parent filters')

    effective_parent = (
        folder_id if folder_id is not None else filters.parent_id
    )

    after_dt = (
        _validate_rfc3339_timestamp(filters.modified_after)
        if filters.modified_after
        else None
    )
    before_dt = (
        _validate_rfc3339_timestamp(filters.modified_before)
        if filters.modified_before
        else None
    )
    if after_dt and before_dt and after_dt > before_dt:
        raise DriveInputError('modified bounds are reversed')

    clauses: list[str] = []

    if effective_parent:
        escaped_parent = escape_drive_literal(effective_parent)
        clauses.append(f"'{escaped_parent}' in parents")

    if filters.exact_name:
        escaped_name = escape_drive_literal(filters.exact_name)
        clauses.append(f"name = '{escaped_name}'")

    if filters.text:
        escaped_text = escape_drive_literal(filters.text)
        clauses.append(
            f"(name contains '{escaped_text}' or "
            f"fullText contains '{escaped_text}')"
        )

    if filters.mime_types:
        mime_clauses = [
            f"mimeType = '{escape_drive_literal(mime)}'"
            for mime in filters.mime_types
        ]
        if len(mime_clauses) == 1:
            clauses.append(mime_clauses[0])
        else:
            clauses.append(f'({" or ".join(mime_clauses)})')

    if filters.modified_after:
        escaped_after = escape_drive_literal(filters.modified_after)
        clauses.append(f"modifiedTime >= '{escaped_after}'")

    if filters.modified_before:
        escaped_before = escape_drive_literal(filters.modified_before)
        clauses.append(f"modifiedTime <= '{escaped_before}'")

    clauses.append('trashed = false')
    return ' and '.join(clauses)
