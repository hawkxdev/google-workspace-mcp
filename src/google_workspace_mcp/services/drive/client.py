"""Call Drive provider methods."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from google_workspace_mcp.common.managed_files import (
    ManagedFileError,
    ManagedFileStore,
)
from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)

from .constants import (
    DRIVE_FILE_FIELDS,
    DRIVE_FOLDER_MIME,
    DRIVE_IO_CHUNK_BYTES,
    DRIVE_LIST_FIELDS,
    EXPORT_FORMATS,
    GOOGLE_WORKSPACE_MIMES,
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_EXPORT_BYTES,
    MAX_DRIVE_FILES,
    MAX_DRIVE_ID_CHARS,
    MAX_DRIVE_NAME_CHARS,
    MAX_DRIVE_PAGE_SIZE,
    MAX_DRIVE_PARENTS,
    MAX_DRIVE_TEXT_CHARS,
    MAX_DRIVE_TOKEN_CHARS,
    REQUEST_RETRIES,
)
from .errors import (
    DriveConflictError,
    DriveInputError,
    DriveManagedFileError,
    DriveProviderError,
    DriveScopeError,
)
from .query import build_drive_query
from .schemas import (
    DriveExportFormat,
    DriveFile,
    DriveFileList,
    DriveManagedFile,
    DriveMutationResult,
    DriveSearchFilters,
)

ServiceBuilder = Callable[[GoogleCredentials], Any]

_SAFE_REASONS = frozenset(
    {
        'appNotAuthorizedToFile',
        'cannotCopyFile',
        'cannotModifyInheritedAccess',
        'conditionNotMet',
        'conflict',
        'dailyLimitExceeded',
        'domainPolicy',
        'duplicate',
        'fileNotDownloadable',
        'fileNotFound',
        'forbidden',
        'insufficientFilePermissions',
        'insufficientPermissions',
        'internalError',
        'invalid',
        'notFound',
        'parentNotFound',
        'rateLimitExceeded',
        'teamDrivesFolderMoveInNotSupported',
        'teamDrivesParentSyncRequired',
        'userRateLimitExceeded',
    }
)


def build_drive_service(credentials: GoogleCredentials) -> Any:
    """Build Drive provider service."""
    return build(
        'drive',
        'v3',
        credentials=credentials.to_google_credentials(),
        cache_discovery=False,
        static_discovery=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    """Require Drive response mapping."""
    if not isinstance(value, Mapping):
        raise DriveProviderError('Drive returned an invalid response')
    return value


def _sequence(value: Any, limit: int) -> Sequence[Any]:
    """Require bounded Drive collection."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise DriveProviderError('Drive returned an invalid response')
    if len(value) > limit:
        raise DriveProviderError('Drive returned an invalid response')
    return value


def _text(value: Any, limit: int = MAX_DRIVE_TEXT_CHARS) -> str:
    """Validate bounded Drive text."""
    if not isinstance(value, str) or len(value) > limit:
        raise DriveProviderError('Drive returned an invalid response')
    return value


def _optional_text(
    value: Any, limit: int = MAX_DRIVE_TEXT_CHARS
) -> str | None:
    """Validate optional bounded text."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise DriveProviderError('Drive returned an invalid response')
    return value


def _integer(value: Any, min_val: int = 0, max_val: int | None = None) -> int:
    """Validate bounded Drive integer."""
    if isinstance(value, bool):
        raise DriveProviderError('Drive returned an invalid response')
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            raise DriveProviderError(
                'Drive returned an invalid response'
            ) from None
    else:
        raise DriveProviderError('Drive returned an invalid response')

    if parsed < min_val or (max_val is not None and parsed > max_val):
        raise DriveProviderError('Drive returned an invalid response')
    return parsed


def _safe_reason(value: Any) -> str:
    """Normalize Drive error reason."""
    if value is None:
        return 'unknown'
    if not isinstance(value, str) or len(value) > 128:
        return 'unknown'
    return value if value in _SAFE_REASONS else 'unknown'


def _file(value: Any) -> DriveFile:
    """Parse Drive file metadata."""
    data = _mapping(value)
    file_id = _text(data.get('id'), MAX_DRIVE_ID_CHARS)
    if not file_id:
        raise DriveProviderError('Drive returned an invalid response')
    name = _text(data.get('name'), MAX_DRIVE_NAME_CHARS)
    if not name:
        raise DriveProviderError('Drive returned an invalid response')
    mime_type = _text(data.get('mimeType'), 255)
    if not mime_type:
        raise DriveProviderError('Drive returned an invalid response')

    raw_size = data.get('size')
    size = _integer(raw_size, min_val=0) if raw_size is not None else None

    created_time = _optional_text(data.get('createdTime'), 128) or ''
    modified_time = _optional_text(data.get('modifiedTime'), 128) or ''

    raw_version = data.get('version')
    version = (
        _integer(raw_version, min_val=0) if raw_version is not None else 0
    )

    raw_parents = data.get('parents')
    if raw_parents is not None:
        parents_seq = _sequence(raw_parents, MAX_DRIVE_PARENTS)
        parsed_parents: list[str] = []
        for p in parents_seq:
            if not isinstance(p, str) or not p or len(p) > MAX_DRIVE_ID_CHARS:
                raise DriveProviderError('Drive returned an invalid response')
            parsed_parents.append(p)
        parents = tuple(parsed_parents)
    else:
        parents = ()

    web_view_link = _optional_text(data.get('webViewLink'), 2_048) or ''
    md5_checksum = _optional_text(data.get('md5Checksum'), 64) or ''
    sha1_checksum = _optional_text(data.get('sha1Checksum'), 64) or ''
    sha256_checksum = _optional_text(data.get('sha256Checksum'), 64) or ''

    raw_trashed = data.get('trashed')
    if raw_trashed is not None and not isinstance(raw_trashed, bool):
        raise DriveProviderError('Drive returned an invalid response')
    trashed = bool(raw_trashed) if raw_trashed is not None else False

    raw_shared = data.get('shared')
    if raw_shared is not None and not isinstance(raw_shared, bool):
        raise DriveProviderError('Drive returned an invalid response')
    shared = bool(raw_shared) if raw_shared is not None else False

    drive_id = _optional_text(data.get('driveId'), MAX_DRIVE_ID_CHARS)

    return DriveFile(
        file_id=file_id,
        name=name,
        mime_type=mime_type,
        size=size,
        created_time=created_time,
        modified_time=modified_time,
        version=version,
        parents=parents,
        web_view_link=web_view_link,
        md5_checksum=md5_checksum,
        sha1_checksum=sha1_checksum,
        sha256_checksum=sha256_checksum,
        trashed=trashed,
        shared=shared,
        drive_id=drive_id,
    )


def _file_list(value: Any) -> DriveFileList:
    """Parse Drive file collection."""
    data = _mapping(value)
    raw_files = data.get('files')
    if raw_files is not None:
        files_seq = _sequence(raw_files, MAX_DRIVE_FILES)
        files = tuple(_file(item) for item in files_seq)
    else:
        files = ()

    raw_token = data.get('nextPageToken')
    next_page_token = (
        _text(raw_token, MAX_DRIVE_TOKEN_CHARS)
        if raw_token is not None
        else ''
    )

    raw_incomplete = data.get('incompleteSearch')
    if raw_incomplete is not None and not isinstance(raw_incomplete, bool):
        raise DriveProviderError('Drive returned an invalid response')
    incomplete_search = (
        bool(raw_incomplete) if raw_incomplete is not None else False
    )

    return DriveFileList(
        files=files,
        next_page_token=next_page_token,
        incomplete_search=incomplete_search,
    )


class DriveGateway:
    """Normalize Drive provider operations."""

    def __init__(
        self,
        store: GoogleCredentialStore,
        service_builder: ServiceBuilder = build_drive_service,
        num_retries: int = REQUEST_RETRIES,
        downloader_factory: Any = MediaIoBaseDownload,
        uploader_factory: Any = MediaIoBaseUpload,
    ) -> None:
        """Initialize Drive provider gateway."""
        self._store = store
        self._service_builder = service_builder
        self._num_retries = num_retries
        self._downloader_factory = downloader_factory
        self._uploader_factory = uploader_factory

    def service(self) -> Any:
        """Build authenticated Drive service."""
        try:
            return self._service_builder(self._store.refresh())
        except DriveProviderError:
            raise
        except Exception:
            raise DriveProviderError(
                'Drive credentials are unavailable'
            ) from None

    @staticmethod
    def _http_reason(error: HttpError) -> str | None:
        """Read safe Drive reason."""
        try:
            raw_content = error.content
            if isinstance(raw_content, bytes):
                raw_content = raw_content.decode('utf-8')
            if isinstance(raw_content, str):
                content = json.loads(raw_content)
                errors = content.get('error', {}).get('errors', [])
                if isinstance(errors, list) and errors:
                    reason = errors[0].get('reason')
                    return reason if isinstance(reason, str) else None
        except AttributeError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        return None

    def _translate_http_error(self, error: HttpError) -> Exception:
        """Translate provider HTTP error."""
        status = int(getattr(error.resp, 'status', 0))
        reason = self._http_reason(error)
        if status == 401:
            return DriveProviderError('Google authorization requires renewal')
        if status == 403 and reason in {
            'insufficientFilePermissions',
            'insufficientPermissions',
            'appNotAuthorizedToFile',
            'cannotModifyInheritedAccess',
            'domainPolicy',
        }:
            return DriveScopeError(
                'Drive operation requires additional permissions'
            )
        if status in {403, 429} and reason in {
            'rateLimitExceeded',
            'userRateLimitExceeded',
            'dailyLimitExceeded',
        }:
            return DriveProviderError('Drive is temporarily rate limited')
        if status == 412:
            return DriveConflictError('Drive file changed since it was read')
        if status == 409:
            return DriveConflictError(
                'Drive request conflicts with existing data'
            )
        message = {
            400: 'Drive rejected the request',
            403: 'Drive request was forbidden',
            404: 'Drive resource was not found',
            429: 'Drive is temporarily rate limited',
        }.get(status, 'Drive request is temporarily unavailable')
        return DriveProviderError(message)

    def execute_raw(self, request: Any) -> Any:
        """Execute raw Drive request."""
        try:
            return request.execute(num_retries=self._num_retries)
        except HttpError as error:
            raise self._translate_http_error(error) from None
        except TransportError, TimeoutError, ConnectionError, OSError:
            raise DriveProviderError(
                'Drive request is temporarily unavailable'
            ) from None

    def execute(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped Drive request."""
        return _mapping(self.execute_raw(request))

    def search_files(
        self,
        filters: DriveSearchFilters,
        page_size: int = MAX_DRIVE_PAGE_SIZE,
        page_token: str | None = None,
    ) -> DriveFileList:
        """Search Drive matching files."""
        bounded_page_size = min(max(1, page_size), MAX_DRIVE_PAGE_SIZE)
        query = build_drive_query(filters)
        kwargs: dict[str, Any] = {
            'q': query,
            'spaces': 'drive',
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
            'fields': DRIVE_LIST_FIELDS,
            'pageSize': bounded_page_size,
        }
        if filters.drive_id:
            kwargs['corpora'] = 'drive'
            kwargs['driveId'] = filters.drive_id
        else:
            kwargs['corpora'] = 'user'

        if page_token:
            kwargs['pageToken'] = page_token

        request = self.service().files().list(**kwargs)
        return _file_list(self.execute_raw(request))

    def get_file(self, file_id: str) -> DriveFile:
        """Retrieve single file metadata."""
        request = (
            self.service()
            .files()
            .get(
                fileId=file_id,
                supportsAllDrives=True,
                fields=DRIVE_FILE_FIELDS,
            )
        )
        return _file(self.execute_raw(request))

    def list_folder(
        self,
        folder_id: str,
        page_size: int = MAX_DRIVE_PAGE_SIZE,
        page_token: str | None = None,
        drive_id: str | None = None,
    ) -> DriveFileList:
        """List folder child files."""
        bounded_page_size = min(max(1, page_size), MAX_DRIVE_PAGE_SIZE)
        filters = DriveSearchFilters(drive_id=drive_id)
        query = build_drive_query(filters, folder_id=folder_id)
        kwargs: dict[str, Any] = {
            'q': query,
            'spaces': 'drive',
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
            'fields': DRIVE_LIST_FIELDS,
            'pageSize': bounded_page_size,
        }
        if drive_id:
            kwargs['corpora'] = 'drive'
            kwargs['driveId'] = drive_id
        else:
            kwargs['corpora'] = 'user'

        if page_token:
            kwargs['pageToken'] = page_token

        request = self.service().files().list(**kwargs)
        return _file_list(self.execute_raw(request))

    def download_file(
        self,
        file_id: str,
        files: ManagedFileStore,
    ) -> DriveManagedFile:
        """Download binary Drive file."""
        file_meta = self.get_file(file_id)
        if (
            file_meta.mime_type in GOOGLE_WORKSPACE_MIMES
            or file_meta.mime_type.startswith('application/vnd.google-apps.')
        ):
            raise DriveInputError(
                'Google Workspace files must be exported, not downloaded'
            )
        if file_meta.size is None or file_meta.size < 0:
            raise DriveInputError('Drive file size is missing or invalid')
        if file_meta.size > MAX_DRIVE_DOWNLOAD_BYTES:
            raise DriveInputError(
                'Drive file exceeds maximum download size of 25 MiB'
            )

        request = (
            self.service()
            .files()
            .get_media(
                fileId=file_id,
                supportsAllDrives=True,
                acknowledgeAbuse=False,
            )
        )

        try:
            with files.writer(
                namespace='drive',
                object_id=file_id,
                original_name=file_meta.name,
                mime_type=file_meta.mime_type,
                expected_size=file_meta.size,
                max_bytes=MAX_DRIVE_DOWNLOAD_BYTES,
            ) as writer:
                downloader = self._downloader_factory(
                    writer, request, chunksize=DRIVE_IO_CHUNK_BYTES
                )
                done = False
                while not done:
                    try:
                        _status, done = downloader.next_chunk(
                            num_retries=self._num_retries
                        )
                    except HttpError as error:
                        raise self._translate_http_error(error) from None
                    except (
                        TransportError,
                        TimeoutError,
                        ConnectionError,
                        OSError,
                    ):
                        raise DriveProviderError(
                            'Drive request is temporarily unavailable'
                        ) from None
                    except ManagedFileError as exc:
                        raise DriveManagedFileError(str(exc)) from exc

                if (
                    file_meta.sha256_checksum
                    and writer.current_sha256.lower()
                    != file_meta.sha256_checksum.lower()
                ):
                    raise DriveManagedFileError('Drive file checksum mismatch')

                try:
                    record = writer.commit()
                except ManagedFileError as exc:
                    raise DriveManagedFileError(str(exc)) from exc

                return DriveManagedFile(
                    managed_name=record.managed_name,
                    original_name=record.original_name,
                    mime_type=record.mime_type,
                    size=record.size,
                    sha256=record.sha256,
                )
        except ManagedFileError as exc:
            raise DriveManagedFileError(str(exc)) from exc

    def export_file(
        self,
        file_id: str,
        export_format: DriveExportFormat | str,
        files: ManagedFileStore,
    ) -> DriveManagedFile:
        """Export Workspace document file."""
        file_meta = self.get_file(file_id)
        if file_meta.mime_type not in EXPORT_FORMATS:
            raise DriveInputError(
                f'File MIME type {file_meta.mime_type} does not support export'
            )

        format_key = str(
            export_format.value
            if isinstance(export_format, DriveExportFormat)
            else export_format
        )
        if format_key not in EXPORT_FORMATS[file_meta.mime_type]:
            raise DriveInputError(
                f'Export format {export_format} is not supported for '
                f'{file_meta.mime_type}'
            )

        spec = EXPORT_FORMATS[file_meta.mime_type][format_key]
        if file_meta.name.endswith(spec.extension):
            original_name = file_meta.name
        else:
            stem = Path(file_meta.name).stem
            original_name = f'{stem}{spec.extension}'

        request = (
            self.service()
            .files()
            .export_media(
                fileId=file_id,
                mimeType=spec.mime_type,
            )
        )

        try:
            with files.writer(
                namespace='drive',
                object_id=file_id,
                original_name=original_name,
                mime_type=spec.mime_type,
                expected_size=None,
                max_bytes=MAX_DRIVE_EXPORT_BYTES,
            ) as writer:
                downloader = self._downloader_factory(
                    writer, request, chunksize=DRIVE_IO_CHUNK_BYTES
                )
                done = False
                while not done:
                    try:
                        _status, done = downloader.next_chunk(
                            num_retries=self._num_retries
                        )
                    except HttpError as error:
                        raise self._translate_http_error(error) from None
                    except (
                        TransportError,
                        TimeoutError,
                        ConnectionError,
                        OSError,
                    ):
                        raise DriveProviderError(
                            'Drive request is temporarily unavailable'
                        ) from None
                    except ManagedFileError as exc:
                        raise DriveManagedFileError(str(exc)) from exc

                try:
                    record = writer.commit()
                except ManagedFileError as exc:
                    raise DriveManagedFileError(str(exc)) from exc

                return DriveManagedFile(
                    managed_name=record.managed_name,
                    original_name=record.original_name,
                    mime_type=record.mime_type,
                    size=record.size,
                    sha256=record.sha256,
                )
        except ManagedFileError as exc:
            raise DriveManagedFileError(str(exc)) from exc

    def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
    ) -> DriveMutationResult:
        """Create Drive parent folder."""
        if not name or not name.strip() or len(name) > MAX_DRIVE_NAME_CHARS:
            raise DriveInputError('Folder name is invalid or too long')
        if parent_id is not None and (
            not parent_id
            or not parent_id.strip()
            or len(parent_id) > MAX_DRIVE_ID_CHARS
        ):
            raise DriveInputError('Parent ID is invalid or too long')

        body: dict[str, Any] = {
            'name': name,
            'mimeType': DRIVE_FOLDER_MIME,
        }
        if parent_id:
            body['parents'] = [parent_id]

        request = (
            self.service()
            .files()
            .create(
                body=body,
                supportsAllDrives=True,
                fields=DRIVE_FILE_FIELDS,
            )
        )
        return DriveMutationResult(file=_file(self.execute_raw(request)))

    def upload_file(
        self,
        managed_name: str,
        expected_size: int,
        expected_sha256: str,
        name: str,
        mime_type: str,
        parent_id: str | None,
        files: ManagedFileStore,
    ) -> DriveMutationResult:
        """Upload managed local file."""
        if expected_size < 0:
            raise DriveInputError('Expected size must be non-negative')
        if expected_size > MAX_DRIVE_DOWNLOAD_BYTES:
            raise DriveInputError(
                'Drive upload exceeds maximum size of 25 MiB'
            )
        if (
            not expected_sha256
            or len(expected_sha256) != 64
            or not all(c in '0123456789abcdefABCDEF' for c in expected_sha256)
        ):
            raise DriveInputError('Expected SHA-256 digest is invalid')
        if not name or not name.strip() or len(name) > MAX_DRIVE_NAME_CHARS:
            raise DriveInputError('File name is invalid or too long')
        if not mime_type or not mime_type.strip() or len(mime_type) > 255:
            raise DriveInputError('MIME type is invalid or too long')
        if parent_id is not None and (
            not parent_id
            or not parent_id.strip()
            or len(parent_id) > MAX_DRIVE_ID_CHARS
        ):
            raise DriveInputError('Parent ID is invalid or too long')

        try:
            with files.open_verified(
                managed_name=managed_name,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            ) as open_file:
                body: dict[str, Any] = {'name': name}
                if parent_id:
                    body['parents'] = [parent_id]

                media = self._uploader_factory(
                    open_file,
                    mimetype=mime_type,
                    resumable=False,
                )
                request = (
                    self.service()
                    .files()
                    .create(
                        body=body,
                        media_body=media,
                        supportsAllDrives=True,
                        fields=DRIVE_FILE_FIELDS,
                    )
                )
                return DriveMutationResult(
                    file=_file(self.execute_raw(request))
                )
        except ManagedFileError as exc:
            raise DriveManagedFileError(str(exc)) from exc

    def _require_version(
        self,
        file_id: str,
        expected_version: int,
    ) -> DriveFile:
        """Verify target file version."""
        if (
            not file_id
            or not file_id.strip()
            or len(file_id) > MAX_DRIVE_ID_CHARS
        ):
            raise DriveInputError('File ID is invalid or too long')
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise DriveInputError(
                'Expected version must be a non-negative integer'
            )

        file_meta = self.get_file(file_id)
        if file_meta.version != expected_version:
            raise DriveConflictError(
                f'Drive file version {file_meta.version} does not match '
                f'expected version {expected_version}'
            )
        return file_meta

    def update_file(
        self,
        file_id: str,
        expected_version: int,
        name: str | None,
        managed_name: str | None,
        expected_size: int | None,
        expected_sha256: str | None,
        mime_type: str | None,
        files: ManagedFileStore,
    ) -> DriveMutationResult:
        """Update Drive file content."""
        has_metadata = name is not None
        content_fields = (
            managed_name,
            expected_size,
            expected_sha256,
            mime_type,
        )
        has_any_content = any(x is not None for x in content_fields)
        has_all_content = all(x is not None for x in content_fields)

        if not has_metadata and not has_any_content:
            raise DriveInputError(
                'File update requires at least one metadata or content change'
            )
        if has_any_content and not has_all_content:
            raise DriveInputError(
                'Content update requires managed_name, expected_size, '
                'expected_sha256, and mime_type'
            )

        if name is not None and (
            not name or not name.strip() or len(name) > MAX_DRIVE_NAME_CHARS
        ):
            raise DriveInputError('File name is invalid or too long')
        if (
            managed_name is not None
            and expected_size is not None
            and expected_sha256 is not None
            and mime_type is not None
        ):
            if (
                not managed_name
                or not managed_name.strip()
                or len(managed_name) > MAX_DRIVE_NAME_CHARS
            ):
                raise DriveInputError(
                    'Managed file name is invalid or too long'
                )
            if expected_size < 0:
                raise DriveInputError('Expected size must be non-negative')
            if expected_size > MAX_DRIVE_DOWNLOAD_BYTES:
                raise DriveInputError(
                    'Drive upload exceeds maximum size of 25 MiB'
                )
            if (
                not expected_sha256
                or len(expected_sha256) != 64
                or not all(
                    c in '0123456789abcdefABCDEF' for c in expected_sha256
                )
            ):
                raise DriveInputError('Expected SHA-256 digest is invalid')
            if not mime_type or not mime_type.strip() or len(mime_type) > 255:
                raise DriveInputError('MIME type is invalid or too long')

        self._require_version(file_id, expected_version)

        body: dict[str, Any] = {}
        if has_metadata and name is not None:
            body['name'] = name

        if (
            managed_name is not None
            and expected_size is not None
            and expected_sha256 is not None
            and mime_type is not None
        ):
            try:
                with files.open_verified(
                    managed_name=managed_name,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                ) as open_file:
                    media = self._uploader_factory(
                        open_file,
                        mimetype=mime_type,
                        resumable=False,
                    )
                    request = (
                        self.service()
                        .files()
                        .update(
                            fileId=file_id,
                            body=body,
                            media_body=media,
                            supportsAllDrives=True,
                            fields=DRIVE_FILE_FIELDS,
                        )
                    )
                    return DriveMutationResult(
                        file=_file(self.execute_raw(request))
                    )
            except ManagedFileError as exc:
                raise DriveManagedFileError(str(exc)) from exc
        else:
            request = (
                self.service()
                .files()
                .update(
                    fileId=file_id,
                    body=body,
                    supportsAllDrives=True,
                    fields=DRIVE_FILE_FIELDS,
                )
            )
            return DriveMutationResult(file=_file(self.execute_raw(request)))

    def move_file(
        self,
        file_id: str,
        expected_version: int,
        destination_parent_id: str,
    ) -> DriveMutationResult:
        """Move Drive file parent."""
        if (
            not destination_parent_id
            or not destination_parent_id.strip()
            or len(destination_parent_id) > MAX_DRIVE_ID_CHARS
        ):
            raise DriveInputError(
                'Destination parent ID is invalid or too long'
            )

        file_meta = self._require_version(file_id, expected_version)

        current_parents = file_meta.parents
        if not current_parents:
            raise DriveInputError('File has no parent to remove')
        if len(current_parents) > MAX_DRIVE_PARENTS:
            raise DriveInputError(
                'File parents collection exceeds maximum limit'
            )
        if (
            len(current_parents) == 1
            and current_parents[0] == destination_parent_id
        ):
            raise DriveInputError('File is already in the destination parent')

        remove_parents = ','.join(current_parents)
        request = (
            self.service()
            .files()
            .update(
                fileId=file_id,
                addParents=destination_parent_id,
                removeParents=remove_parents,
                supportsAllDrives=True,
                fields=DRIVE_FILE_FIELDS,
            )
        )
        return DriveMutationResult(file=_file(self.execute_raw(request)))

    def copy_file(
        self,
        file_id: str,
        name: str | None = None,
        parent_id: str | None = None,
    ) -> DriveMutationResult:
        """Copy Drive source file."""
        if (
            not file_id
            or not file_id.strip()
            or len(file_id) > MAX_DRIVE_ID_CHARS
        ):
            raise DriveInputError('File ID is invalid or too long')
        if name is not None and (
            not name or not name.strip() or len(name) > MAX_DRIVE_NAME_CHARS
        ):
            raise DriveInputError('File name is invalid or too long')
        if parent_id is not None and (
            not parent_id
            or not parent_id.strip()
            or len(parent_id) > MAX_DRIVE_ID_CHARS
        ):
            raise DriveInputError('Parent ID is invalid or too long')

        body: dict[str, Any] = {}
        if name is not None:
            body['name'] = name
        if parent_id is not None:
            body['parents'] = [parent_id]

        request = (
            self.service()
            .files()
            .copy(
                fileId=file_id,
                body=body,
                supportsAllDrives=True,
                fields=DRIVE_FILE_FIELDS,
            )
        )
        return DriveMutationResult(file=_file(self.execute_raw(request)))
