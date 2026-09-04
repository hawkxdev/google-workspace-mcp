"""Drive download behavior tests."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import httplib2  # type: ignore[import-untyped]
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from google_workspace_mcp.common.managed_files import (
    ManagedFileStore,
)
from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.services.drive.client import (
    DriveGateway,
)
from google_workspace_mcp.services.drive.constants import (
    _DOCX_MIME,
    _PPTX_MIME,
    _XLSX_MIME,
    DRIVE_FOLDER_MIME,
    DRIVE_SCOPES,
    EXPORT_FORMATS,
    GOOGLE_DOC_MIME,
    GOOGLE_DRAWING_MIME,
    GOOGLE_SHEET_MIME,
    GOOGLE_SLIDE_MIME,
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_EXPORT_BYTES,
)
from google_workspace_mcp.services.drive.errors import (
    DriveInputError,
    DriveManagedFileError,
    DriveProviderError,
    DriveScopeError,
)
from google_workspace_mcp.services.drive.schemas import (
    DriveExportFormat,
    DriveManagedFile,
)


class FakeRequest:
    """Record Drive request execution."""

    def __init__(
        self,
        value: Any = None,
        chunks: list[Any] | None = None,
    ) -> None:
        """Initialize test double."""
        self.value = value
        self.chunks = chunks or []
        self.retries: list[int] = []
        self.uri = 'https://drive.googleapis.com/drive/v3/fake'

    def execute(self, num_retries: int = 0) -> Any:
        """Execute prepared resource."""
        self.retries.append(num_retries)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeDownloader:
    """Simulate MediaIoBaseDownload chunk streaming."""

    def __init__(
        self,
        fd: Any,
        request: FakeRequest,
        chunksize: int = 1024 * 1024,
    ) -> None:
        """Initialize test double."""
        self.fd = fd
        self.request = request
        self.chunksize = chunksize
        self.chunks = list(request.chunks)
        self.index = 0
        self.retries: list[int] = []

    def next_chunk(self, num_retries: int = 0) -> tuple[Any, bool]:
        """Return download chunk."""
        self.retries.append(num_retries)
        if self.index < len(self.chunks):
            chunk = self.chunks[self.index]
            self.index += 1
            if isinstance(chunk, Exception):
                raise chunk
            self.fd.write(chunk)
            done = self.index >= len(self.chunks)
            return None, done
        return None, True


class FakeFilesEndpoint:
    """Record endpoint calls."""

    def __init__(self) -> None:
        """Initialize test double."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.media_chunks: dict[str, list[Any]] = {}

    def get(self, **kwargs: Any) -> FakeRequest:
        """Get fake get."""
        self.calls.append(('get', kwargs))
        file_id = kwargs.get('fileId', 'default')
        fallback = self.responses.get('get', {})
        resp = self.responses.get(f'get_{file_id}', fallback)
        return FakeRequest(resp)

    def get_media(self, **kwargs: Any) -> FakeRequest:
        """Get fake media."""
        self.calls.append(('get_media', kwargs))
        file_id = kwargs.get('fileId', 'default')
        chunks = self.media_chunks.get(
            f'get_media_{file_id}',
            self.media_chunks.get('get_media', [b'data']),
        )
        return FakeRequest(chunks=chunks)

    def export_media(self, **kwargs: Any) -> FakeRequest:
        """Export fake media."""
        self.calls.append(('export_media', kwargs))
        file_id = kwargs.get('fileId', 'default')
        mime_type = kwargs.get('mimeType', 'default')
        default_chunks = self.media_chunks.get(
            f'export_media_{file_id}',
            self.media_chunks.get('export_media', [b'exported_data']),
        )
        chunks = self.media_chunks.get(
            f'export_media_{file_id}_{mime_type}',
            default_chunks,
        )
        return FakeRequest(chunks=chunks)


class FakeDriveService:
    """Expose fake Drive endpoints."""

    def __init__(self) -> None:
        """Initialize test double."""
        self.files_endpoint = FakeFilesEndpoint()

    def files(self) -> FakeFilesEndpoint:
        """Return files resource."""
        return self.files_endpoint


class FakeStore(GoogleCredentialStore):
    """Return Drive test credentials."""

    def __init__(self) -> None:
        """Initialize test double."""
        self.credentials = GoogleCredentials(
            token='access_token',
            scopes=DRIVE_SCOPES,
        )

    def refresh(self, request: Any = None) -> GoogleCredentials:
        """Refresh fake resource."""
        return self.credentials


def _valid_file_payload(
    file_id: str = 'file_123',
    name: str = 'sample.png',
    mime_type: str = 'image/png',
    size: int = 1024,
    sha256_checksum: str = '',
) -> dict[str, Any]:
    """Build valid file payload."""
    return {
        'id': file_id,
        'name': name,
        'mimeType': mime_type,
        'size': str(size),
        'createdTime': '2026-08-25T10:00:00.000Z',
        'modifiedTime': '2026-08-25T11:00:00.000Z',
        'version': '1',
        'parents': ['folder_root'],
        'webViewLink': 'https://drive.google.com/file/d/file_123/view',
        'md5Checksum': 'md5_123',
        'sha1Checksum': 'sha1_123',
        'sha256Checksum': sha256_checksum,
        'trashed': False,
        'shared': False,
    }


def _make_http_error(
    status: int, reason: str | None = None, message: str = 'Error'
) -> HttpError:
    """Build HTTP error."""
    resp = httplib2.Response({'status': str(status)})
    body: dict[str, Any] = {'error': {'message': message}}
    if reason is not None:
        body['error']['errors'] = [{'reason': reason}]
    content = json.dumps(body).encode('utf-8')
    return HttpError(resp, content)


@pytest.fixture
def managed_store(tmp_path: Path) -> ManagedFileStore:
    """Provide managed store."""
    store_dir = tmp_path / 'downloads'
    store_dir.mkdir(mode=0o700)
    return ManagedFileStore(store_dir)


# ============================================================================
# Step 1: Download Tests
# ============================================================================


def test_download_file_metadata_preflight(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    content = b'binary_image_content'
    content_hash = hashlib.sha256(content).hexdigest()
    service.files_endpoint.responses['get_file_1'] = _valid_file_payload(
        file_id='file_1',
        name='photo.png',
        mime_type='image/png',
        size=len(content),
        sha256_checksum=content_hash,
    )
    service.files_endpoint.media_chunks['get_media_file_1'] = [content]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    result = gateway.download_file('file_1', managed_store)

    assert service.files_endpoint.calls[0] == (
        'get',
        {
            'fileId': 'file_1',
            'supportsAllDrives': True,
            'fields': (
                'id,name,mimeType,size,createdTime,modifiedTime,version,'
                'parents,webViewLink,md5Checksum,sha1Checksum,'
                'sha256Checksum,trashed,shared,driveId'
            ),
        },
    )
    assert service.files_endpoint.calls[1] == (
        'get_media',
        {
            'fileId': 'file_1',
            'supportsAllDrives': True,
            'acknowledgeAbuse': False,
        },
    )
    assert isinstance(result, DriveManagedFile)
    assert result.original_name == 'photo.png'
    assert result.mime_type == 'image/png'
    assert result.size == len(content)
    assert result.sha256 == content_hash

    target = managed_store.directory / result.managed_name
    assert target.exists()
    assert target.read_bytes() == content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    'workspace_mime',
    [
        GOOGLE_DOC_MIME,
        GOOGLE_SHEET_MIME,
        GOOGLE_SLIDE_MIME,
        GOOGLE_DRAWING_MIME,
        DRIVE_FOLDER_MIME,
        'application/vnd.google-apps.form',
        'application/vnd.google-apps.site',
    ],
)
def test_download_file_rejects_google_workspace_mimes(
    workspace_mime: str,
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_doc_1'] = _valid_file_payload(
        file_id='doc_1',
        name='My Document',
        mime_type=workspace_mime,
        size=1024,
    )

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveInputError, match='export'):
        gateway.download_file('doc_1', managed_store)

    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


@pytest.mark.parametrize(
    'invalid_size,expected_exc',
    [
        (None, DriveInputError),
        (-1, DriveProviderError),
    ],
)
def test_download_file_rejects_missing_or_invalid_size(
    invalid_size: int | None,
    expected_exc: type[Exception],
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    payload = _valid_file_payload(
        file_id='bin_1',
        name='data.bin',
        mime_type='application/octet-stream',
    )
    if invalid_size is None:
        payload.pop('size', None)
    else:
        payload['size'] = str(invalid_size)
    service.files_endpoint.responses['get_bin_1'] = payload

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(expected_exc):
        gateway.download_file('bin_1', managed_store)

    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


def test_download_file_rejects_size_exceeding_25mib(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_big_1'] = _valid_file_payload(
        file_id='big_1',
        name='video.mp4',
        mime_type='video/mp4',
        size=MAX_DRIVE_DOWNLOAD_BYTES + 1,
    )

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises((DriveInputError, DriveManagedFileError)):
        gateway.download_file('big_1', managed_store)

    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


def test_download_file_chunked_streaming(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    chunks = [b'chunk_one_', b'chunk_two_', b'chunk_three']
    total_data = b''.join(chunks)
    service.files_endpoint.responses['get_multi_1'] = _valid_file_payload(
        file_id='multi_1',
        name='archive.tar',
        mime_type='application/x-tar',
        size=len(total_data),
    )
    service.files_endpoint.media_chunks['get_media_multi_1'] = chunks

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    result = gateway.download_file('multi_1', managed_store)

    assert result.size == len(total_data)
    assert result.sha256 == hashlib.sha256(total_data).hexdigest()
    published = managed_store.directory / result.managed_name
    assert published.read_bytes() == total_data

    no_temp = all(
        not p.name.startswith('.tmp_')
        for p in managed_store.directory.iterdir()
    )
    assert no_temp


def test_download_file_chunk_overflow_cleans_up(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_overflow_1'] = _valid_file_payload(
        file_id='overflow_1',
        name='overflow.bin',
        mime_type='application/octet-stream',
        size=10,
    )
    payload_chunk = b'0123456789extra_bytes'
    service.files_endpoint.media_chunks['get_media_overflow_1'] = [
        payload_chunk
    ]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveManagedFileError):
        gateway.download_file('overflow_1', managed_store)

    assert list(managed_store.directory.iterdir()) == []


def test_download_file_declared_size_mismatch_cleans_up(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_short_1'] = _valid_file_payload(
        file_id='short_1',
        name='short.bin',
        mime_type='application/octet-stream',
        size=100,
    )
    service.files_endpoint.media_chunks['get_media_short_1'] = [b'short_data']

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveManagedFileError):
        gateway.download_file('short_1', managed_store)

    assert list(managed_store.directory.iterdir()) == []


def test_download_file_sha256_checksum_mismatch_cleans_up(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    content = b'valid_content'
    bad_hash = '0' * 64
    service.files_endpoint.responses['get_badhash_1'] = _valid_file_payload(
        file_id='badhash_1',
        name='data.bin',
        mime_type='application/octet-stream',
        size=len(content),
        sha256_checksum=bad_hash,
    )
    service.files_endpoint.media_chunks['get_media_badhash_1'] = [content]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveManagedFileError, match='checksum'):
        gateway.download_file('badhash_1', managed_store)

    assert list(managed_store.directory.iterdir()) == []


@pytest.mark.parametrize(
    'http_error,expected_exc',
    [
        (_make_http_error(401), DriveProviderError),
        (
            _make_http_error(403, reason='insufficientFilePermissions'),
            DriveScopeError,
        ),
        (_make_http_error(404), DriveProviderError),
        (
            _make_http_error(429, reason='rateLimitExceeded'),
            DriveProviderError,
        ),
        (_make_http_error(500), DriveProviderError),
    ],
)
def test_download_file_provider_http_error_cleans_up(
    http_error: HttpError,
    expected_exc: type[Exception],
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_err_1'] = _valid_file_payload(
        file_id='err_1',
        name='data.bin',
        mime_type='application/octet-stream',
        size=100,
    )
    service.files_endpoint.media_chunks['get_media_err_1'] = [http_error]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(expected_exc):
        gateway.download_file('err_1', managed_store)

    assert list(managed_store.directory.iterdir()) == []


def test_download_file_transport_error_cleans_up(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_trans_1'] = _valid_file_payload(
        file_id='trans_1',
        name='data.bin',
        mime_type='application/octet-stream',
        size=100,
    )
    service.files_endpoint.media_chunks['get_media_trans_1'] = [
        TransportError('connection lost')
    ]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveProviderError, match='temporarily unavailable'):
        gateway.download_file('trans_1', managed_store)

    assert list(managed_store.directory.iterdir()) == []


# ============================================================================
# Step 2: Export Tests
# ============================================================================


@pytest.mark.parametrize(
    'source_mime,export_format,expected_mime,expected_ext',
    [
        (GOOGLE_DOC_MIME, DriveExportFormat.PDF, 'application/pdf', '.pdf'),
        (GOOGLE_DOC_MIME, DriveExportFormat.DOCX, _DOCX_MIME, '.docx'),
        (GOOGLE_DOC_MIME, DriveExportFormat.TXT, 'text/plain', '.txt'),
        (GOOGLE_DOC_MIME, DriveExportFormat.HTML, 'application/zip', '.zip'),
        (GOOGLE_SHEET_MIME, DriveExportFormat.PDF, 'application/pdf', '.pdf'),
        (GOOGLE_SHEET_MIME, DriveExportFormat.XLSX, _XLSX_MIME, '.xlsx'),
        (GOOGLE_SHEET_MIME, DriveExportFormat.CSV, 'text/csv', '.csv'),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.PDF, 'application/pdf', '.pdf'),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.PPTX, _PPTX_MIME, '.pptx'),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.TXT, 'text/plain', '.txt'),
        (
            GOOGLE_DRAWING_MIME,
            DriveExportFormat.PDF,
            'application/pdf',
            '.pdf',
        ),
        (GOOGLE_DRAWING_MIME, DriveExportFormat.PNG, 'image/png', '.png'),
        (GOOGLE_DRAWING_MIME, DriveExportFormat.SVG, 'image/svg+xml', '.svg'),
    ],
)
def test_export_file_all_approved_source_format_pairs(
    source_mime: str,
    export_format: DriveExportFormat,
    expected_mime: str,
    expected_ext: str,
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    content = b'exported_document_bytes'
    service.files_endpoint.responses['get_exp_1'] = _valid_file_payload(
        file_id='exp_1',
        name='Document Title',
        mime_type=source_mime,
        size=0,
    )
    chunk_key = f'export_media_exp_1_{expected_mime}'
    service.files_endpoint.media_chunks[chunk_key] = [content]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    result = gateway.export_file('exp_1', export_format, managed_store)

    assert service.files_endpoint.calls[0] == (
        'get',
        {
            'fileId': 'exp_1',
            'supportsAllDrives': True,
            'fields': (
                'id,name,mimeType,size,createdTime,modifiedTime,version,'
                'parents,webViewLink,md5Checksum,sha1Checksum,'
                'sha256Checksum,trashed,shared,driveId'
            ),
        },
    )
    assert service.files_endpoint.calls[1] == (
        'export_media',
        {
            'fileId': 'exp_1',
            'mimeType': expected_mime,
        },
    )
    assert isinstance(result, DriveManagedFile)
    assert result.mime_type == expected_mime
    assert result.original_name == f'Document Title{expected_ext}'
    assert result.size == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()

    target = managed_store.directory / result.managed_name
    assert target.exists()
    assert target.read_bytes() == content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    'non_exportable_mime',
    [
        'image/png',
        'application/pdf',
        'text/plain',
        DRIVE_FOLDER_MIME,
        'application/vnd.google-apps.form',
        'application/vnd.google-apps.site',
    ],
)
def test_export_file_rejects_non_exportable_source_mime(
    non_exportable_mime: str,
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_nonexp_1'] = _valid_file_payload(
        file_id='nonexp_1',
        name='Photo',
        mime_type=non_exportable_mime,
        size=1024,
    )

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveInputError, match='does not support export'):
        gateway.export_file('nonexp_1', DriveExportFormat.PDF, managed_store)

    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


@pytest.mark.parametrize(
    'source_mime,invalid_format',
    [
        (GOOGLE_DOC_MIME, DriveExportFormat.XLSX),
        (GOOGLE_DOC_MIME, DriveExportFormat.CSV),
        (GOOGLE_DOC_MIME, DriveExportFormat.PPTX),
        (GOOGLE_DOC_MIME, DriveExportFormat.PNG),
        (GOOGLE_DOC_MIME, DriveExportFormat.SVG),
        (GOOGLE_SHEET_MIME, DriveExportFormat.DOCX),
        (GOOGLE_SHEET_MIME, DriveExportFormat.HTML),
        (GOOGLE_SHEET_MIME, DriveExportFormat.PPTX),
        (GOOGLE_SHEET_MIME, DriveExportFormat.PNG),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.DOCX),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.XLSX),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.CSV),
        (GOOGLE_SLIDE_MIME, DriveExportFormat.HTML),
        (GOOGLE_DRAWING_MIME, DriveExportFormat.DOCX),
        (GOOGLE_DRAWING_MIME, DriveExportFormat.XLSX),
        (GOOGLE_DRAWING_MIME, DriveExportFormat.TXT),
        (GOOGLE_DRAWING_MIME, DriveExportFormat.PPTX),
    ],
)
def test_export_file_rejects_unsupported_format_for_source(
    source_mime: str,
    invalid_format: DriveExportFormat,
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_mismatch_1'] = _valid_file_payload(
        file_id='mismatch_1',
        name='File Title',
        mime_type=source_mime,
        size=0,
    )

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveInputError, match='not supported'):
        gateway.export_file('mismatch_1', invalid_format, managed_store)

    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


def test_export_file_10mib_overflow_cleans_up(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_bigexp_1'] = _valid_file_payload(
        file_id='bigexp_1',
        name='Huge Sheet',
        mime_type=GOOGLE_SHEET_MIME,
        size=0,
    )
    chunk = b'x' * (MAX_DRIVE_EXPORT_BYTES + 1)
    service.files_endpoint.media_chunks['export_media_bigexp_1'] = [chunk]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveManagedFileError):
        gateway.export_file('bigexp_1', DriveExportFormat.PDF, managed_store)

    assert list(managed_store.directory.iterdir()) == []


@pytest.mark.parametrize(
    'original_name,format_choice,expected_name',
    [
        ('Report', DriveExportFormat.PDF, 'Report.pdf'),
        ('Report.gdoc', DriveExportFormat.PDF, 'Report.pdf'),
        ('Report.pdf', DriveExportFormat.PDF, 'Report.pdf'),
        ('Spreadsheet', DriveExportFormat.CSV, 'Spreadsheet.csv'),
        ('Spreadsheet.gsheet', DriveExportFormat.XLSX, 'Spreadsheet.xlsx'),
        ('Slides.pptx', DriveExportFormat.PDF, 'Slides.pdf'),
    ],
)
def test_export_file_deterministic_extension(
    original_name: str,
    format_choice: DriveExportFormat,
    expected_name: str,
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    source_mime = (
        GOOGLE_SHEET_MIME
        if format_choice in {DriveExportFormat.CSV, DriveExportFormat.XLSX}
        else (
            GOOGLE_SLIDE_MIME if 'Slides' in original_name else GOOGLE_DOC_MIME
        )
    )
    service.files_endpoint.responses['get_name_1'] = _valid_file_payload(
        file_id='name_1',
        name=original_name,
        mime_type=source_mime,
        size=0,
    )
    service.files_endpoint.media_chunks['export_media_name_1'] = [b'content']

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    result = gateway.export_file('name_1', format_choice, managed_store)

    assert result.original_name == expected_name


def test_export_file_provider_http_error_cleans_up(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    service.files_endpoint.responses['get_experr_1'] = _valid_file_payload(
        file_id='experr_1',
        name='Doc',
        mime_type=GOOGLE_DOC_MIME,
        size=0,
    )
    service.files_endpoint.media_chunks['export_media_experr_1'] = [
        _make_http_error(404, message='File not found')
    ]

    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: service,
        downloader_factory=FakeDownloader,
    )

    with pytest.raises(DriveProviderError):
        gateway.export_file('experr_1', DriveExportFormat.PDF, managed_store)

    assert list(managed_store.directory.iterdir()) == []


def test_export_format_csv_first_sheet_description() -> None:
    assert 'csv' in EXPORT_FORMATS[GOOGLE_SHEET_MIME]
    assert EXPORT_FORMATS[GOOGLE_SHEET_MIME]['csv'].extension == '.csv'
    assert EXPORT_FORMATS[GOOGLE_SHEET_MIME]['csv'].mime_type == 'text/csv'
    assert DriveExportFormat.CSV.value == 'csv'
