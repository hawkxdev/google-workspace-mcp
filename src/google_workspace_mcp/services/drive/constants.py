"""Define Drive service limits."""

from __future__ import annotations

from dataclasses import dataclass

DRIVE_SCOPES = (
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file',
)

MAX_DRIVE_PAGE_SIZE = 50
MAX_DRIVE_FILES = 50
MAX_DRIVE_PARENTS = 100
MAX_DRIVE_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_DRIVE_EXPORT_BYTES = 10 * 1024 * 1024
DRIVE_IO_CHUNK_BYTES = 1024 * 1024
MAX_DRIVE_TEXT_CHARS = 4_000
MAX_DRIVE_NAME_CHARS = 255
MAX_DRIVE_ID_CHARS = 256
MAX_DRIVE_TOKEN_CHARS = 2_048
MAX_DRIVE_MIME_TYPES = 20
REQUEST_RETRIES = 2

DRIVE_FILE_FIELDS = (
    'id,name,mimeType,size,createdTime,modifiedTime,version,parents,'
    'webViewLink,md5Checksum,sha1Checksum,sha256Checksum,trashed,shared,driveId'
)
DRIVE_LIST_FIELDS = (
    f'files({DRIVE_FILE_FIELDS}),nextPageToken,incompleteSearch'
)

DRIVE_FOLDER_MIME = 'application/vnd.google-apps.folder'
GOOGLE_DOC_MIME = 'application/vnd.google-apps.document'
GOOGLE_SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
GOOGLE_SLIDE_MIME = 'application/vnd.google-apps.presentation'
GOOGLE_DRAWING_MIME = 'application/vnd.google-apps.drawing'

GOOGLE_WORKSPACE_MIMES = frozenset(
    {
        DRIVE_FOLDER_MIME,
        GOOGLE_DOC_MIME,
        GOOGLE_SHEET_MIME,
        GOOGLE_SLIDE_MIME,
        GOOGLE_DRAWING_MIME,
    }
)

_DOCX_MIME = (
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
)
_XLSX_MIME = (
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
)
_PPTX_MIME = (
    'application/vnd.openxmlformats-officedocument.presentationml.presentation'
)


@dataclass(frozen=True, slots=True)
class ExportSpec:
    """Describe export format target."""

    mime_type: str
    extension: str


EXPORT_FORMATS: dict[str, dict[str, ExportSpec]] = {
    GOOGLE_DOC_MIME: {
        'pdf': ExportSpec('application/pdf', '.pdf'),
        'docx': ExportSpec(_DOCX_MIME, '.docx'),
        'txt': ExportSpec('text/plain', '.txt'),
        'html': ExportSpec('application/zip', '.zip'),
    },
    GOOGLE_SHEET_MIME: {
        'pdf': ExportSpec('application/pdf', '.pdf'),
        'xlsx': ExportSpec(_XLSX_MIME, '.xlsx'),
        'csv': ExportSpec('text/csv', '.csv'),
    },
    GOOGLE_SLIDE_MIME: {
        'pdf': ExportSpec('application/pdf', '.pdf'),
        'pptx': ExportSpec(_PPTX_MIME, '.pptx'),
        'txt': ExportSpec('text/plain', '.txt'),
    },
    GOOGLE_DRAWING_MIME: {
        'pdf': ExportSpec('application/pdf', '.pdf'),
        'png': ExportSpec('image/png', '.png'),
        'svg': ExportSpec('image/svg+xml', '.svg'),
    },
}
