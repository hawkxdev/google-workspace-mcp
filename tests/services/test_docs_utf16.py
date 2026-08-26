"""Test Docs UTF-16 semantics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from google_workspace_mcp.services.docs.constants import (
    DOCS_SCOPES,
    MAX_DOCS_BATCH_OPERATIONS,
    MAX_DOCS_BLOCKS,
    MAX_DOCS_ID_CHARS,
    MAX_DOCS_OUTPUT_CHARS,
    MAX_DOCS_REPLACEMENTS,
    MAX_DOCS_TAB_DEPTH,
    MAX_DOCS_TABS,
    MAX_DOCS_TEXT_CHARS,
    MAX_DOCS_TITLE_CHARS,
    REQUEST_RETRIES,
)
from google_workspace_mcp.services.docs.errors import (
    DocsConflictError,
    DocsError,
    DocsInputError,
    DocsNotFoundError,
    DocsProviderError,
    DocsRateLimitError,
    DocsScopeError,
    DocsUnsupportedError,
)
from google_workspace_mcp.services.docs.schemas import DocsModel, DocsSegment
from google_workspace_mcp.services.docs.structure import (
    utf16_length,
    validate_delete_range,
    validate_insert_index,
    validate_utf16_boundary,
)

EMOJI = '\U0001f604'


def units(text: str) -> int:
    """Count units for tests."""
    return len(text.encode('utf-16-le')) // 2


def segment(text: str, start_index: int = 1) -> DocsSegment:
    """Build test document segment."""
    return DocsSegment(
        tab_id='tab-1',
        start_index=start_index,
        end_index=start_index + units(text),
        text=text,
    )


def test_utf16_emoji_consumes_two_units() -> None:
    assert utf16_length(f'A{EMOJI}B') == 4


def test_utf16_ascii_length_matches_character_count() -> None:
    assert utf16_length('Hello') == 5


def test_utf16_length_of_empty_string_is_zero() -> None:
    assert utf16_length('') == 0


def test_utf16_length_counts_newline_and_bmp_characters() -> None:
    assert utf16_length('Привет\n') == 7


def test_utf16_boundary_rejects_surrogate_split() -> None:
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        validate_utf16_boundary(f'A{EMOJI}B', 2)


@pytest.mark.parametrize('index', [0, 1, 2, 3, 4, 5])
def test_utf16_boundary_accepts_every_ascii_position(index: int) -> None:
    validate_utf16_boundary('Hello', index)


@pytest.mark.parametrize('index', [0, 1, 3, 4])
def test_utf16_boundary_accepts_positions_around_emoji(index: int) -> None:
    validate_utf16_boundary(f'A{EMOJI}B', index)


def test_utf16_boundary_accepts_segment_end() -> None:
    validate_utf16_boundary(f'A{EMOJI}B', 4)


def test_utf16_boundary_rejects_negative_index() -> None:
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        validate_utf16_boundary('Hello', -1)


def test_utf16_boundary_rejects_index_past_end() -> None:
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        validate_utf16_boundary(f'A{EMOJI}B', 5)


def test_insert_index_accepts_segment_start_and_end() -> None:
    body = segment('Hello\n')
    validate_insert_index(body, body.start_index)
    validate_insert_index(body, body.end_index)


def test_insert_index_rejects_position_before_segment() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='segment'):
        validate_insert_index(body, body.start_index - 1)


def test_insert_index_rejects_position_after_segment() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='segment'):
        validate_insert_index(body, body.end_index + 1)


def test_insert_index_rejects_split_surrogate_pair() -> None:
    body = segment(f'A{EMOJI}B\n')
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        validate_insert_index(body, body.start_index + 2)


def test_delete_range_accepts_half_open_interval() -> None:
    body = segment('Hello\n')
    validate_delete_range(body, body.start_index, body.start_index + 1)


def test_delete_range_accepts_everything_before_terminal_newline() -> None:
    body = segment('Hello\n')
    validate_delete_range(body, body.start_index, body.end_index - 1)


def test_delete_range_rejects_terminal_newline() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='terminal newline'):
        validate_delete_range(body, body.start_index, body.end_index)


def test_delete_range_rejects_terminal_newline_only() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='terminal newline'):
        validate_delete_range(body, body.end_index - 1, body.end_index)


def test_delete_range_rejects_equal_bounds() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='range'):
        validate_delete_range(body, body.start_index, body.start_index)


def test_delete_range_rejects_reversed_bounds() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='range'):
        validate_delete_range(body, body.start_index + 2, body.start_index)


def test_delete_range_rejects_start_before_segment() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='segment'):
        validate_delete_range(body, body.start_index - 1, body.start_index + 1)


def test_delete_range_rejects_end_past_segment() -> None:
    body = segment('Hello\n')
    with pytest.raises(DocsInputError, match='segment'):
        validate_delete_range(body, body.start_index, body.end_index + 1)


def test_delete_range_rejects_start_splitting_emoji() -> None:
    body = segment(f'A{EMOJI}B\n')
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        validate_delete_range(body, body.start_index + 2, body.end_index - 1)


def test_delete_range_rejects_end_splitting_emoji() -> None:
    body = segment(f'A{EMOJI}B\n')
    with pytest.raises(DocsInputError, match='UTF-16 boundary'):
        validate_delete_range(body, body.start_index, body.start_index + 2)


def test_delete_range_accepts_boundaries_around_emoji() -> None:
    body = segment(f'A{EMOJI}B\n')
    validate_delete_range(body, body.start_index + 1, body.start_index + 3)


def test_constants_definitions() -> None:
    assert DOCS_SCOPES == (
        'https://www.googleapis.com/auth/documents',
        'https://www.googleapis.com/auth/drive.file',
    )
    assert MAX_DOCS_BATCH_OPERATIONS == 20
    assert MAX_DOCS_BLOCKS == 100
    assert MAX_DOCS_OUTPUT_CHARS == 20_000
    assert MAX_DOCS_ID_CHARS == 256
    assert MAX_DOCS_TITLE_CHARS == 256
    assert MAX_DOCS_TEXT_CHARS == 50_000
    assert MAX_DOCS_REPLACEMENTS == 1_000
    assert REQUEST_RETRIES == 3


def test_structural_bounds_are_defined() -> None:
    assert MAX_DOCS_TABS == 200
    assert MAX_DOCS_TAB_DEPTH == 10


def test_error_hierarchy() -> None:
    assert issubclass(DocsInputError, DocsError)
    assert issubclass(DocsNotFoundError, DocsError)
    assert issubclass(DocsProviderError, DocsError)
    assert issubclass(DocsRateLimitError, DocsError)
    assert issubclass(DocsConflictError, DocsError)
    assert issubclass(DocsScopeError, DocsError)
    assert issubclass(DocsUnsupportedError, DocsError)


def test_docs_model_forbids_extra_and_is_frozen() -> None:
    class DummyModel(DocsModel):
        """Provide dummy test model."""

        title: str

    instance = DummyModel(title='test')
    assert instance.title == 'test'
    with pytest.raises(ValidationError):
        DummyModel(title='test', extra_field='invalid')  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        instance.title = 'modified'  # type: ignore[misc]
