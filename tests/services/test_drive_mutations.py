"""Test Drive file mutations: versioned update, move, and copy."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, BinaryIO

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
    DRIVE_FILE_FIELDS,
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_ID_CHARS,
    MAX_DRIVE_NAME_CHARS,
    MAX_DRIVE_PARENTS,
)
from google_workspace_mcp.services.drive.errors import (
    DriveConflictError,
    DriveInputError,
    DriveManagedFileError,
    DriveProviderError,
    DriveScopeError,
)
from google_workspace_mcp.services.drive.schemas import (
    DriveFile,
    DriveMutationResult,
)


class FakeRequest:
    """Record Drive request execution."""

    def __init__(
        self,
        value: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.value = value
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0) -> Any:
        self.retries.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.value


class FakeFilesEndpoint:
    """Record Drive files endpoint calls."""

    def __init__(self) -> None:
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        if not self.responses[method]:
            raise AssertionError(
                f'Unexpected {method} call with kwargs={kwargs}'
            )
        response = self.responses[method].popleft()
        if isinstance(response, Exception):
            req = FakeRequest(error=response)
        else:
            req = FakeRequest(value=response)
        self.calls.append((method, kwargs, req))
        return req

    def get(self, **kwargs: Any) -> FakeRequest:
        return self._call('get', kwargs)

    def update(self, **kwargs: Any) -> FakeRequest:
        return self._call('update', kwargs)

    def copy(self, **kwargs: Any) -> FakeRequest:
        return self._call('copy', kwargs)


class FakeDriveService:
    """Provide fake Drive API service."""

    def __init__(self) -> None:
        self.files_endpoint = FakeFilesEndpoint()

    def files(self) -> FakeFilesEndpoint:
        return self.files_endpoint


class FakeStore(GoogleCredentialStore):
    """Provide fake credential store for testing."""

    def __init__(self) -> None:
        pass

    def refresh(self, request: Any = None) -> GoogleCredentials:
        return GoogleCredentials(token='fake_token')


class FakeUploader:
    """Record MediaIoBaseUpload parameters."""

    def __init__(
        self,
        fd: BinaryIO,
        mimetype: str,
        resumable: bool = False,
        chunksize: int | None = None,
    ) -> None:
        self.fd = fd
        self.mimetype = mimetype
        self.resumable = resumable
        self.chunksize = chunksize
        self.read_content = fd.read()


@pytest.fixture
def managed_store(tmp_path: Path) -> ManagedFileStore:
    return ManagedFileStore(directory=tmp_path)


def _valid_file_payload(
    file_id: str = 'file_123',
    name: str = 'Document.pdf',
    version: int = 5,
    parents: tuple[str, ...] = ('folder_parent_1',),
    mime_type: str = 'application/pdf',
    size: int = 1024,
) -> dict[str, Any]:
    return {
        'id': file_id,
        'name': name,
        'mimeType': mime_type,
        'size': str(size),
        'createdTime': '2026-08-25T10:00:00Z',
        'modifiedTime': '2026-08-25T12:00:00Z',
        'version': str(version),
        'parents': list(parents),
        'webViewLink': f'https://drive.google.com/file/d/{file_id}/view',
        'md5Checksum': '0123456789abcdef0123456789abcdef',
        'sha1Checksum': '0123456789abcdef0123456789abcdef01234567',
        'sha256Checksum': 'a' * 64,
        'trashed': False,
        'shared': False,
        'driveId': 'drive_abc',
    }


def _http_error(status: int, reason: str = 'unknown') -> HttpError:
    resp = httplib2.Response({'status': str(status)})
    content = json.dumps(
        {
            'error': {
                'code': status,
                'message': f'HTTP {status} {reason}',
                'errors': [{'reason': reason, 'message': reason}],
            }
        }
    ).encode('utf-8')
    return HttpError(resp, content)


def _seed_managed_file(
    store: ManagedFileStore,
    name: str = 'source.pdf',
    content: bytes = b'%PDF-1.4 test payload',
    mime_type: str = 'application/pdf',
) -> tuple[str, int, str]:
    digest = hashlib.sha256(content).hexdigest()
    with store.writer(
        namespace='drive',
        object_id='test_obj',
        original_name=name,
        mime_type=mime_type,
        expected_size=len(content),
    ) as writer:
        writer.write(content)
        record = writer.commit()
    return record.managed_name, len(content), digest


# ============================================================================
# Step 1: Version Preflight Tests
# ============================================================================


def test_require_version_matches_and_returns_file() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload(file_id='f_1', version=42)
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    file_meta = gateway._require_version('f_1', 42)

    assert isinstance(file_meta, DriveFile)
    assert file_meta.file_id == 'f_1'
    assert file_meta.version == 42
    assert len(service.files_endpoint.calls) == 1
    method, kwargs, _ = service.files_endpoint.calls[0]
    assert method == 'get'
    assert kwargs['fileId'] == 'f_1'
    assert kwargs['supportsAllDrives'] is True
    assert kwargs['fields'] == DRIVE_FILE_FIELDS


def test_require_version_mismatch_raises_conflict() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload(file_id='f_1', version=43)
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveConflictError, match='version.*43.*42'):
        gateway._require_version('f_1', 42)


def test_require_version_malformed_version_rejected() -> None:
    service = FakeDriveService()
    store = FakeStore()
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='Expected version'):
        gateway._require_version('f_1', -1)

    with pytest.raises(DriveInputError, match='Expected version'):
        gateway._require_version('f_1', 'not_an_int')  # type: ignore[arg-type]

    with pytest.raises(DriveInputError, match='Expected version'):
        gateway._require_version('f_1', True)  # type: ignore[arg-type]


def test_require_version_invalid_file_id_rejected() -> None:
    service = FakeDriveService()
    store = FakeStore()
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='File ID'):
        gateway._require_version('', 1)

    with pytest.raises(DriveInputError, match='File ID'):
        gateway._require_version('   ', 1)

    with pytest.raises(DriveInputError, match='File ID'):
        gateway._require_version('x' * (MAX_DRIVE_ID_CHARS + 1), 1)


def test_require_version_docstring_documents_version_check() -> None:
    doc = DriveGateway._require_version.__doc__ or ''
    assert 'version' in doc.lower()


# ============================================================================
# Step 2: Update Tests
# ============================================================================


def test_update_file_metadata_only_success(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', name='Old.txt', version=10)
    updated = _valid_file_payload(file_id='f_up', name='New.txt', version=11)
    service.files_endpoint.queue('get', preflight)
    service.files_endpoint.queue('update', updated)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    result = gateway.update_file(
        file_id='f_up',
        expected_version=10,
        name='New.txt',
        managed_name=None,
        expected_size=None,
        expected_sha256=None,
        mime_type=None,
        files=managed_store,
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.name == 'New.txt'
    assert result.file.version == 11

    assert len(service.files_endpoint.calls) == 2
    get_call, update_call = service.files_endpoint.calls
    assert get_call[0] == 'get'
    assert get_call[1]['fileId'] == 'f_up'
    assert get_call[1]['supportsAllDrives'] is True

    assert update_call[0] == 'update'
    assert update_call[1]['fileId'] == 'f_up'
    assert update_call[1]['body'] == {'name': 'New.txt'}
    assert update_call[1]['supportsAllDrives'] is True
    assert update_call[1]['fields'] == DRIVE_FILE_FIELDS
    assert 'media_body' not in update_call[1]


def test_update_file_content_only_success(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(
        file_id='f_up', name='Orig.pdf', version=10, size=500
    )
    updated = _valid_file_payload(
        file_id='f_up', name='Orig.pdf', version=11, size=21
    )
    service.files_endpoint.queue('get', preflight)
    service.files_endpoint.queue('update', updated)

    mname, size, sha256 = _seed_managed_file(
        managed_store, name='Orig.pdf', content=b'%PDF-1.4 updated bytes'
    )
    gateway = DriveGateway(
        store,
        service_builder=lambda _: service,
        uploader_factory=FakeUploader,
    )

    result = gateway.update_file(
        file_id='f_up',
        expected_version=10,
        name=None,
        managed_name=mname,
        expected_size=size,
        expected_sha256=sha256,
        mime_type='application/pdf',
        files=managed_store,
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.version == 11
    assert len(service.files_endpoint.calls) == 2
    update_call = service.files_endpoint.calls[1]
    assert update_call[0] == 'update'
    assert update_call[1]['fileId'] == 'f_up'
    assert update_call[1]['supportsAllDrives'] is True
    assert update_call[1]['fields'] == DRIVE_FILE_FIELDS
    uploader = update_call[1]['media_body']
    assert isinstance(uploader, FakeUploader)
    assert uploader.mimetype == 'application/pdf'
    assert uploader.resumable is False
    assert uploader.read_content == b'%PDF-1.4 updated bytes'


def test_update_file_combined_metadata_and_content(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', name='Old.pdf', version=1)
    updated = _valid_file_payload(file_id='f_up', name='New.pdf', version=2)
    service.files_endpoint.queue('get', preflight)
    service.files_endpoint.queue('update', updated)

    mname, size, sha256 = _seed_managed_file(
        managed_store, name='New.pdf', content=b'new content'
    )
    gateway = DriveGateway(
        store,
        service_builder=lambda _: service,
        uploader_factory=FakeUploader,
    )

    result = gateway.update_file(
        file_id='f_up',
        expected_version=1,
        name='New.pdf',
        managed_name=mname,
        expected_size=size,
        expected_sha256=sha256,
        mime_type='application/pdf',
        files=managed_store,
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.name == 'New.pdf'
    assert result.file.version == 2
    update_call = service.files_endpoint.calls[1]
    assert update_call[1]['body'] == {'name': 'New.pdf'}
    assert isinstance(update_call[1]['media_body'], FakeUploader)


def test_update_file_requires_at_least_one_change(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', version=1)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='at least one'):
        gateway.update_file(
            file_id='f_up',
            expected_version=1,
            name=None,
            managed_name=None,
            expected_size=None,
            expected_sha256=None,
            mime_type=None,
            files=managed_store,
        )


def test_update_file_content_fields_all_or_none(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', version=1)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='Content update requires'):
        gateway.update_file(
            file_id='f_up',
            expected_version=1,
            name=None,
            managed_name='m_123',
            expected_size=100,
            expected_sha256=None,
            mime_type='application/pdf',
            files=managed_store,
        )


def test_update_file_version_mismatch_fails_before_write(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', version=10)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveConflictError, match='version.*10.*5'):
        gateway.update_file(
            file_id='f_up',
            expected_version=5,
            name='NewName.txt',
            managed_name=None,
            expected_size=None,
            expected_sha256=None,
            mime_type=None,
            files=managed_store,
        )
    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


def test_update_file_name_validation(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', version=1)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='File name'):
        gateway.update_file(
            file_id='f_up',
            expected_version=1,
            name='',
            managed_name=None,
            expected_size=None,
            expected_sha256=None,
            mime_type=None,
            files=managed_store,
        )

    service.files_endpoint.queue('get', preflight)
    with pytest.raises(DriveInputError, match='File name'):
        gateway.update_file(
            file_id='f_up',
            expected_version=1,
            name='x' * (MAX_DRIVE_NAME_CHARS + 1),
            managed_name=None,
            expected_size=None,
            expected_sha256=None,
            mime_type=None,
            files=managed_store,
        )


def test_update_file_managed_file_not_found(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', version=1)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveManagedFileError):
        gateway.update_file(
            file_id='f_up',
            expected_version=1,
            name=None,
            managed_name='nonexistent.bin',
            expected_size=10,
            expected_sha256='a' * 64,
            mime_type='application/octet-stream',
            files=managed_store,
        )


def test_update_file_oversized_content_rejected(
    managed_store: ManagedFileStore,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_up', version=1)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='25 MiB'):
        gateway.update_file(
            file_id='f_up',
            expected_version=1,
            name=None,
            managed_name='large.bin',
            expected_size=MAX_DRIVE_DOWNLOAD_BYTES + 1,
            expected_sha256='a' * 64,
            mime_type='application/octet-stream',
            files=managed_store,
        )


def test_update_file_docstring_documents_file_content() -> None:
    doc = DriveGateway.update_file.__doc__ or ''
    assert 'content' in doc.lower()


# ============================================================================
# Step 3: Move Tests
# ============================================================================


def test_move_file_success() -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(
        file_id='f_mv',
        parents=('p_old_1', 'p_old_2'),
        version=7,
    )
    moved = _valid_file_payload(
        file_id='f_mv',
        parents=('p_dest',),
        version=8,
    )
    service.files_endpoint.queue('get', preflight)
    service.files_endpoint.queue('update', moved)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    result = gateway.move_file(
        file_id='f_mv',
        expected_version=7,
        destination_parent_id='p_dest',
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.parents == ('p_dest',)
    assert result.file.version == 8

    assert len(service.files_endpoint.calls) == 2
    get_call, update_call = service.files_endpoint.calls
    assert get_call[0] == 'get'
    assert get_call[1]['fileId'] == 'f_mv'

    assert update_call[0] == 'update'
    assert update_call[1]['fileId'] == 'f_mv'
    assert update_call[1]['addParents'] == 'p_dest'
    assert update_call[1]['removeParents'] == 'p_old_1,p_old_2'
    assert update_call[1]['supportsAllDrives'] is True
    assert update_call[1]['fields'] == DRIVE_FILE_FIELDS


def test_move_file_same_single_parent_rejected_as_noop() -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(
        file_id='f_mv',
        parents=('p_dest',),
        version=7,
    )
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='already in the destination'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=7,
            destination_parent_id='p_dest',
        )
    assert len(service.files_endpoint.calls) == 1


def test_move_file_empty_parents_rejected() -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(
        file_id='f_mv',
        parents=(),
        version=7,
    )
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='no parent'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=7,
            destination_parent_id='p_dest',
        )


def test_move_file_oversized_parents_rejected() -> None:
    service = FakeDriveService()
    store = FakeStore()
    many_parents = tuple(f'p_{i}' for i in range(MAX_DRIVE_PARENTS + 1))
    payload = _valid_file_payload(
        file_id='f_mv',
        parents=many_parents,
        version=7,
    )
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveProviderError, match='invalid response'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=7,
            destination_parent_id='p_dest',
        )


def test_move_file_destination_parent_validation() -> None:
    service = FakeDriveService()
    store = FakeStore()
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='Destination parent'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=1,
            destination_parent_id='',
        )

    with pytest.raises(DriveInputError, match='Destination parent'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=1,
            destination_parent_id='   ',
        )

    with pytest.raises(DriveInputError, match='Destination parent'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=1,
            destination_parent_id='x' * (MAX_DRIVE_ID_CHARS + 1),
        )


def test_move_file_version_mismatch_fails_before_write() -> None:
    service = FakeDriveService()
    store = FakeStore()
    preflight = _valid_file_payload(file_id='f_mv', version=10)
    service.files_endpoint.queue('get', preflight)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveConflictError, match='version.*10.*9'):
        gateway.move_file(
            file_id='f_mv',
            expected_version=9,
            destination_parent_id='p_new',
        )
    assert len(service.files_endpoint.calls) == 1
    assert service.files_endpoint.calls[0][0] == 'get'


def test_move_file_docstring_documents_file_parent() -> None:
    doc = DriveGateway.move_file.__doc__ or ''
    assert 'parent' in doc.lower()


# ============================================================================
# Step 4: Copy Tests
# ============================================================================


def test_copy_file_success_default_params() -> None:
    service = FakeDriveService()
    store = FakeStore()
    copied = _valid_file_payload(file_id='f_copy', name='Copy of Doc.pdf')
    service.files_endpoint.queue('copy', copied)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    result = gateway.copy_file(file_id='f_orig')

    assert isinstance(result, DriveMutationResult)
    assert result.file.file_id == 'f_copy'
    assert result.file.name == 'Copy of Doc.pdf'

    assert len(service.files_endpoint.calls) == 1
    method, kwargs, _ = service.files_endpoint.calls[0]
    assert method == 'copy'
    assert kwargs['fileId'] == 'f_orig'
    assert kwargs['body'] == {}
    assert kwargs['supportsAllDrives'] is True
    assert kwargs['fields'] == DRIVE_FILE_FIELDS


def test_copy_file_with_name_and_parent() -> None:
    service = FakeDriveService()
    store = FakeStore()
    copied = _valid_file_payload(
        file_id='f_copy',
        name='Custom Copy.pdf',
        parents=('p_target',),
    )
    service.files_endpoint.queue('copy', copied)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    result = gateway.copy_file(
        file_id='f_orig',
        name='Custom Copy.pdf',
        parent_id='p_target',
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.file_id == 'f_copy'
    assert result.file.name == 'Custom Copy.pdf'
    assert result.file.parents == ('p_target',)

    assert len(service.files_endpoint.calls) == 1
    method, kwargs, _ = service.files_endpoint.calls[0]
    assert method == 'copy'
    assert kwargs['fileId'] == 'f_orig'
    assert kwargs['body'] == {
        'name': 'Custom Copy.pdf',
        'parents': ['p_target'],
    }
    assert kwargs['supportsAllDrives'] is True
    assert kwargs['fields'] == DRIVE_FILE_FIELDS


def test_copy_file_input_validation() -> None:
    service = FakeDriveService()
    store = FakeStore()
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveInputError, match='File ID'):
        gateway.copy_file(file_id='')

    with pytest.raises(DriveInputError, match='File ID'):
        gateway.copy_file(file_id='x' * (MAX_DRIVE_ID_CHARS + 1))

    with pytest.raises(DriveInputError, match='File name'):
        gateway.copy_file(file_id='f1', name='')

    with pytest.raises(DriveInputError, match='File name'):
        gateway.copy_file(file_id='f1', name='x' * (MAX_DRIVE_NAME_CHARS + 1))

    with pytest.raises(DriveInputError, match='Parent ID'):
        gateway.copy_file(file_id='f1', parent_id='')

    with pytest.raises(DriveInputError, match='Parent ID'):
        gateway.copy_file(
            file_id='f1', parent_id='x' * (MAX_DRIVE_ID_CHARS + 1)
        )


def test_copy_file_scope_denial_mapping() -> None:
    service = FakeDriveService()
    store = FakeStore()
    err = _http_error(403, reason='appNotAuthorizedToFile')
    service.files_endpoint.queue('copy', err)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveScopeError,
        match='Drive operation requires additional permissions',
    ):
        gateway.copy_file(file_id='inaccessible_file')


def test_copy_file_not_found_mapping() -> None:
    service = FakeDriveService()
    store = FakeStore()
    err = _http_error(404, reason='notFound')
    service.files_endpoint.queue('copy', err)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive resource was not found'
    ):
        gateway.copy_file(file_id='missing_file')


def test_copy_file_transport_error() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'copy', TransportError('Connection reset by peer')
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive request is temporarily unavailable'
    ):
        gateway.copy_file(file_id='f1')
