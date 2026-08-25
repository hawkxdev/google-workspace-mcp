"""Define Drive service schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .constants import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_FILES,
    MAX_DRIVE_ID_CHARS,
    MAX_DRIVE_MIME_TYPES,
    MAX_DRIVE_NAME_CHARS,
    MAX_DRIVE_PARENTS,
    MAX_DRIVE_TEXT_CHARS,
    MAX_DRIVE_TOKEN_CHARS,
)


class DriveModel(BaseModel):
    """Configure Drive schema model."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class DriveExportFormat(StrEnum):
    """Select Drive export format."""

    PDF = 'pdf'
    DOCX = 'docx'
    TXT = 'txt'
    HTML = 'html'
    XLSX = 'xlsx'
    CSV = 'csv'
    PPTX = 'pptx'
    PNG = 'png'
    SVG = 'svg'


class DriveFile(DriveModel):
    """Describe Drive file metadata."""

    file_id: str = Field(min_length=1, max_length=MAX_DRIVE_ID_CHARS)
    name: str = Field(min_length=1, max_length=MAX_DRIVE_NAME_CHARS)
    mime_type: str = Field(min_length=1, max_length=255)
    size: int | None = Field(default=None, ge=0)
    created_time: str = Field(default='', max_length=128)
    modified_time: str = Field(default='', max_length=128)
    version: int = Field(default=0, ge=0)
    parents: tuple[str, ...] = Field(default=(), max_length=MAX_DRIVE_PARENTS)
    web_view_link: str = Field(default='', max_length=2_048)
    md5_checksum: str = Field(default='', max_length=64)
    sha1_checksum: str = Field(default='', max_length=64)
    sha256_checksum: str = Field(default='', max_length=64)
    trashed: bool = False
    shared: bool = False
    drive_id: str | None = Field(default=None, max_length=MAX_DRIVE_ID_CHARS)


class DriveFileList(DriveModel):
    """Describe Drive file collection."""

    files: tuple[DriveFile, ...] = Field(
        default=(), max_length=MAX_DRIVE_FILES
    )
    next_page_token: str = Field(default='', max_length=MAX_DRIVE_TOKEN_CHARS)
    incomplete_search: bool = False


class DriveManagedFile(DriveModel):
    """Describe managed Drive file."""

    managed_name: str = Field(min_length=1, max_length=255)
    original_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=MAX_DRIVE_DOWNLOAD_BYTES)
    sha256: str = Field(min_length=64, max_length=64)


class DriveMutationResult(DriveModel):
    """Describe Drive mutation result."""

    file: DriveFile


class DriveSearchFilters(DriveModel):
    """Specify Drive search filters."""

    text: str | None = Field(
        default=None, min_length=1, max_length=MAX_DRIVE_TEXT_CHARS
    )
    exact_name: str | None = Field(
        default=None, min_length=1, max_length=MAX_DRIVE_NAME_CHARS
    )
    parent_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_DRIVE_ID_CHARS
    )
    mime_types: tuple[str, ...] = Field(
        default=(), max_length=MAX_DRIVE_MIME_TYPES
    )
    modified_after: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    modified_before: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    drive_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_DRIVE_ID_CHARS
    )
