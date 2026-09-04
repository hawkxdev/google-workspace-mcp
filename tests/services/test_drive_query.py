"""Drive query behavior tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from google_workspace_mcp.services.drive.constants import (
    DRIVE_FILE_FIELDS,
    DRIVE_FOLDER_MIME,
    DRIVE_IO_CHUNK_BYTES,
    DRIVE_LIST_FIELDS,
    DRIVE_SCOPES,
    EXPORT_FORMATS,
    GOOGLE_DOC_MIME,
    GOOGLE_DRAWING_MIME,
    GOOGLE_SHEET_MIME,
    GOOGLE_SLIDE_MIME,
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_EXPORT_BYTES,
    MAX_DRIVE_FILES,
    MAX_DRIVE_PAGE_SIZE,
    MAX_DRIVE_PARENTS,
)
from google_workspace_mcp.services.drive.errors import (
    DriveConflictError,
    DriveError,
    DriveInputError,
    DriveManagedFileError,
    DriveProviderError,
    DriveScopeError,
)
from google_workspace_mcp.services.drive.query import (
    build_drive_query,
    escape_drive_literal,
)
from google_workspace_mcp.services.drive.schemas import (
    DriveExportFormat,
    DriveFile,
    DriveFileList,
    DriveManagedFile,
    DriveMutationResult,
    DriveSearchFilters,
)


def test_query_escapes_drive_literals() -> None:
    filters = DriveSearchFilters(text="owner's \\ notes")
    query = build_drive_query(filters)
    assert "owner\\'s \\\\ notes" in query
    assert query.endswith('trashed = false')


def test_escape_drive_literal_helper() -> None:
    escaped = escape_drive_literal("hello 'world' \\ test")
    assert escaped == "hello \\'world\\' \\\\ test"
    assert escape_drive_literal('') == ''
    assert escape_drive_literal('plain_text') == 'plain_text'


def test_query_empty_filters_produces_only_trashed() -> None:
    filters = DriveSearchFilters()
    query = build_drive_query(filters)
    assert query == 'trashed = false'


def test_query_folder_id_kwarg() -> None:
    filters = DriveSearchFilters()
    query = build_drive_query(filters, folder_id='folder123')
    assert query == "'folder123' in parents and trashed = false"


def test_query_parent_id_in_filters() -> None:
    filters = DriveSearchFilters(parent_id='parent_abc')
    query = build_drive_query(filters)
    assert query == "'parent_abc' in parents and trashed = false"


def test_query_folder_id_matching_parent_id() -> None:
    filters = DriveSearchFilters(parent_id='same_id')
    query = build_drive_query(filters, folder_id='same_id')
    assert query == "'same_id' in parents and trashed = false"


def test_query_folder_id_conflicting_parent_id() -> None:
    filters = DriveSearchFilters(parent_id='parent_1')
    with pytest.raises(DriveInputError, match='conflicting'):
        build_drive_query(filters, folder_id='parent_2')


def test_query_escapes_parent_ids() -> None:
    filters = DriveSearchFilters(parent_id="folder's \\ id")
    query = build_drive_query(filters)
    assert query == "'folder\\'s \\\\ id' in parents and trashed = false"


def test_query_exact_name() -> None:
    filters = DriveSearchFilters(exact_name="project 'budget' \\ 2026.xlsx")
    query = build_drive_query(filters)
    expected = (
        "name = 'project \\'budget\\' \\\\ 2026.xlsx' and trashed = false"
    )
    assert query == expected


def test_query_text_search_alternatives() -> None:
    filters = DriveSearchFilters(text='quarterly report')
    query = build_drive_query(filters)
    expected = (
        "(name contains 'quarterly report' or "
        "fullText contains 'quarterly report') and "
        'trashed = false'
    )
    assert query == expected


def test_query_single_mime_type() -> None:
    filters = DriveSearchFilters(mime_types=('image/png',))
    query = build_drive_query(filters)
    assert query == "mimeType = 'image/png' and trashed = false"


def test_query_multiple_mime_types() -> None:
    filters = DriveSearchFilters(
        mime_types=('image/png', 'image/jpeg', 'image/webp')
    )
    query = build_drive_query(filters)
    expected = (
        "(mimeType = 'image/png' or mimeType = 'image/jpeg' or "
        "mimeType = 'image/webp') and trashed = false"
    )
    assert query == expected


def test_query_escapes_mime_types() -> None:
    filters = DriveSearchFilters(mime_types=("custom/'mime'\\type",))
    query = build_drive_query(filters)
    expected = "mimeType = 'custom/\\'mime\\'\\\\type' and trashed = false"
    assert query == expected


def test_query_strict_modified_bounds() -> None:
    filters = DriveSearchFilters(
        modified_after='2026-08-01T00:00:00Z',
        modified_before='2026-08-25T23:59:59Z',
    )
    query = build_drive_query(filters)
    expected = (
        "modifiedTime >= '2026-08-01T00:00:00Z' and "
        "modifiedTime <= '2026-08-25T23:59:59Z' and "
        'trashed = false'
    )
    assert query == expected


def test_query_offset_aware_modified_bounds() -> None:
    filters = DriveSearchFilters(
        modified_after='2026-08-01T12:00:00+03:00',
        modified_before='2026-08-25T15:30:00-07:00',
    )
    query = build_drive_query(filters)
    assert "modifiedTime >= '2026-08-01T12:00:00+03:00'" in query
    assert "modifiedTime <= '2026-08-25T15:30:00-07:00'" in query


def test_query_rejects_invalid_rfc3339_modified_after() -> None:
    filters = DriveSearchFilters(modified_after='2026-08-01')
    with pytest.raises(DriveInputError, match='RFC3339'):
        build_drive_query(filters)


def test_query_rejects_invalid_rfc3339_modified_before() -> None:
    filters = DriveSearchFilters(modified_before='not-a-timestamp')
    with pytest.raises(DriveInputError, match='RFC3339'):
        build_drive_query(filters)


def test_query_rejects_naive_timestamp() -> None:
    filters = DriveSearchFilters(modified_after='2026-08-01T12:00:00')
    with pytest.raises(DriveInputError, match='RFC3339'):
        build_drive_query(filters)


def test_query_rejects_reversed_modified_bounds() -> None:
    filters = DriveSearchFilters(
        modified_after='2026-08-25T00:00:00Z',
        modified_before='2026-08-01T00:00:00Z',
    )
    with pytest.raises(DriveInputError, match='reversed|before'):
        build_drive_query(filters)


def test_query_combined_predicates_order() -> None:
    filters = DriveSearchFilters(
        parent_id='root_folder',
        exact_name='report.pdf',
        text='financials',
        mime_types=('application/pdf',),
        modified_after='2026-08-01T00:00:00Z',
        modified_before='2026-08-25T00:00:00Z',
    )
    query = build_drive_query(filters)
    expected = (
        "'root_folder' in parents and "
        "name = 'report.pdf' and "
        "(name contains 'financials' or fullText contains 'financials') and "
        "mimeType = 'application/pdf' and "
        "modifiedTime >= '2026-08-01T00:00:00Z' and "
        "modifiedTime <= '2026-08-25T00:00:00Z' and "
        'trashed = false'
    )
    assert query == expected


def test_drive_constants_exact_values() -> None:
    assert DRIVE_SCOPES == (
        'https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/drive.file',
    )
    assert MAX_DRIVE_PAGE_SIZE == 50
    assert MAX_DRIVE_FILES == 50
    assert MAX_DRIVE_PARENTS == 100
    assert MAX_DRIVE_DOWNLOAD_BYTES == 25 * 1024 * 1024
    assert MAX_DRIVE_EXPORT_BYTES == 10 * 1024 * 1024
    assert DRIVE_IO_CHUNK_BYTES == 1024 * 1024
    assert DRIVE_FILE_FIELDS == (
        'id,name,mimeType,size,createdTime,modifiedTime,version,parents,'
        'webViewLink,md5Checksum,sha1Checksum,sha256Checksum,trashed,shared,driveId'
    )
    expected_list_fields = (
        f'files({DRIVE_FILE_FIELDS}),nextPageToken,incompleteSearch'
    )
    assert expected_list_fields == DRIVE_LIST_FIELDS
    assert DRIVE_FOLDER_MIME == 'application/vnd.google-apps.folder'


def test_export_formats_matrix_completeness() -> None:
    assert set(EXPORT_FORMATS.keys()) == {
        GOOGLE_DOC_MIME,
        GOOGLE_SHEET_MIME,
        GOOGLE_SLIDE_MIME,
        GOOGLE_DRAWING_MIME,
    }

    docx_mime = (
        'application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document'
    )
    xlsx_mime = (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    pptx_mime = (
        'application/vnd.openxmlformats-officedocument.'
        'presentationml.presentation'
    )

    docs_targets = EXPORT_FORMATS[GOOGLE_DOC_MIME]
    assert docs_targets[DriveExportFormat.PDF].mime_type == 'application/pdf'
    assert docs_targets[DriveExportFormat.PDF].extension == '.pdf'
    assert docs_targets[DriveExportFormat.DOCX].mime_type == docx_mime
    assert docs_targets[DriveExportFormat.DOCX].extension == '.docx'
    assert docs_targets[DriveExportFormat.TXT].mime_type == 'text/plain'
    assert docs_targets[DriveExportFormat.TXT].extension == '.txt'
    assert docs_targets[DriveExportFormat.HTML].mime_type == 'application/zip'
    assert docs_targets[DriveExportFormat.HTML].extension == '.zip'

    sheets_targets = EXPORT_FORMATS[GOOGLE_SHEET_MIME]
    assert sheets_targets[DriveExportFormat.PDF].mime_type == 'application/pdf'
    assert sheets_targets[DriveExportFormat.PDF].extension == '.pdf'
    assert sheets_targets[DriveExportFormat.XLSX].mime_type == xlsx_mime
    assert sheets_targets[DriveExportFormat.XLSX].extension == '.xlsx'
    assert sheets_targets[DriveExportFormat.CSV].mime_type == 'text/csv'
    assert sheets_targets[DriveExportFormat.CSV].extension == '.csv'

    slides_targets = EXPORT_FORMATS[GOOGLE_SLIDE_MIME]
    assert slides_targets[DriveExportFormat.PDF].mime_type == 'application/pdf'
    assert slides_targets[DriveExportFormat.PDF].extension == '.pdf'
    assert slides_targets[DriveExportFormat.PPTX].mime_type == pptx_mime
    assert slides_targets[DriveExportFormat.PPTX].extension == '.pptx'
    assert slides_targets[DriveExportFormat.TXT].mime_type == 'text/plain'
    assert slides_targets[DriveExportFormat.TXT].extension == '.txt'

    drawings_targets = EXPORT_FORMATS[GOOGLE_DRAWING_MIME]
    assert (
        drawings_targets[DriveExportFormat.PDF].mime_type == 'application/pdf'
    )
    assert drawings_targets[DriveExportFormat.PDF].extension == '.pdf'
    assert drawings_targets[DriveExportFormat.PNG].mime_type == 'image/png'
    assert drawings_targets[DriveExportFormat.PNG].extension == '.png'
    assert drawings_targets[DriveExportFormat.SVG].mime_type == 'image/svg+xml'
    assert drawings_targets[DriveExportFormat.SVG].extension == '.svg'


def test_drive_errors_hierarchy() -> None:
    assert issubclass(DriveInputError, DriveError)
    assert issubclass(DriveManagedFileError, DriveError)
    assert issubclass(DriveProviderError, DriveError)
    assert issubclass(DriveConflictError, DriveError)
    assert issubclass(DriveScopeError, DriveError)


def test_drive_schemas_frozen_and_forbidden_extra() -> None:
    file = DriveFile(
        file_id='file_123',
        name='doc.txt',
        mime_type='text/plain',
        size=1024,
    )
    assert file.file_id == 'file_123'
    assert file.name == 'doc.txt'
    assert file.mime_type == 'text/plain'
    assert file.size == 1024
    assert file.trashed is False
    assert file.shared is False
    assert file.version == 0
    assert file.parents == ()

    with pytest.raises(ValidationError):
        DriveFile(
            file_id='f1',
            name='d.txt',
            mime_type='text/plain',
            extra_field='invalid',  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        file.name = 'new_name.txt'  # type: ignore[misc]


def test_drive_managed_file_schema() -> None:
    managed = DriveManagedFile(
        managed_name='m_123.bin',
        original_name='download.bin',
        mime_type='application/octet-stream',
        size=100,
        sha256='e' * 64,
    )
    assert managed.size == 100
    assert managed.sha256 == 'e' * 64

    with pytest.raises(ValidationError):
        DriveManagedFile(
            managed_name='m.bin',
            original_name='d.bin',
            mime_type='application/octet-stream',
            size=MAX_DRIVE_DOWNLOAD_BYTES + 1,
            sha256='e' * 64,
        )


def test_drive_mutation_result_schema() -> None:
    file = DriveFile(
        file_id='f1',
        name='folder_a',
        mime_type=DRIVE_FOLDER_MIME,
    )
    result = DriveMutationResult(file=file)
    assert result.file.file_id == 'f1'
    assert result.file.mime_type == DRIVE_FOLDER_MIME


def test_drive_file_list_schema() -> None:
    file = DriveFile(file_id='f1', name='f1', mime_type='text/plain')
    file_list = DriveFileList(
        files=(file,),
        next_page_token='token_123',
        incomplete_search=True,
    )
    assert len(file_list.files) == 1
    assert file_list.next_page_token == 'token_123'
    assert file_list.incomplete_search is True
