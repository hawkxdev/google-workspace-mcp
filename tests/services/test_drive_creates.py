"""Drive creation behavior tests."""

from __future__ import annotations

import hashlib
import json
import os
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
    DRIVE_FOLDER_MIME,
    DRIVE_SCOPES,
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_NAME_CHARS,
)
from google_workspace_mcp.services.drive.errors import (
    DriveInputError,
    DriveManagedFileError,
    DriveProviderError,
    DriveScopeError,
)
from google_workspace_mcp.services.drive.schemas import (
    DriveMutationResult,
)


class FakeRequest:
    """Record Drive request execution."""

    def __init__(
        self,
        value: Any = None,
        error: Exception | None = None,
    ) -> None:
        """Initialize test double."""
        self.value = value
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0) -> Any:
        """Execute prepared resource."""
        self.retries.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.value


class FakeFilesEndpoint:
    """Record endpoint calls."""

    def __init__(self) -> None:
        """Initialize test double."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        """Queue provider resource."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record provider call."""
        if not self.responses[method]:
            raise AssertionError(f'No response queued for files().{method}()')
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def create(self, **kwargs: Any) -> FakeRequest:
        """Create fake create."""
        return self._call('create', kwargs)

    def get(self, **kwargs: Any) -> FakeRequest:
        """Get fake get."""
        return self._call('get', kwargs)


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
            token='test-drive-token',
            scopes=DRIVE_SCOPES,
        )

    def refresh(self, request: Any = None) -> GoogleCredentials:
        """Refresh fake resource."""
        return self.credentials


class FakeUploader:
    """Record upload construction."""

    instances: list[FakeUploader] = []

    def __init__(
        self,
        fd: BinaryIO,
        mimetype: str,
        chunksize: int = 104857600,
        resumable: bool = False,
    ) -> None:
        """Initialize test double."""
        self.fd = fd
        self.mimetype = mimetype
        self.chunksize = chunksize
        self.resumable = resumable
        self.was_open_at_init = not fd.closed
        # Step: Verify open descriptor readability
        pos = fd.tell()
        self.captured_content = fd.read()
        fd.seek(pos)
        FakeUploader.instances.append(self)


def _file_payload(
    file_id: str = 'file_123',
    name: str = 'item_name',
    mime_type: str = 'text/plain',
    size: int = 1024,
    parents: list[str] | None = None,
) -> dict[str, Any]:
    """Build file payload."""
    return {
        'id': file_id,
        'name': name,
        'mimeType': mime_type,
        'size': size,
        'createdTime': '2026-08-25T10:00:00Z',
        'modifiedTime': '2026-08-25T10:00:00Z',
        'version': 1,
        'parents': parents or [],
        'webViewLink': f'https://drive.google.com/file/d/{file_id}/view',
        'md5Checksum': 'md5-123',
        'sha1Checksum': 'sha1-123',
        'sha256Checksum': 'sha256-123',
        'trashed': False,
        'shared': False,
    }


def _http_error(status: int, reason: str = 'error') -> HttpError:
    """Build HTTP error."""
    content = json.dumps(
        {
            'error': {
                'code': status,
                'message': reason,
                'errors': [{'reason': reason}],
            }
        }
    ).encode('utf-8')
    resp = httplib2.Response({'status': str(status)})
    return HttpError(resp, content)


@pytest.fixture(autouse=True)
def _reset_fake_uploader() -> None:
    """Reset fake uploader."""
    FakeUploader.instances.clear()


@pytest.fixture
def managed_store(tmp_path: Path) -> ManagedFileStore:
    """Provide managed store."""
    return ManagedFileStore(directory=tmp_path)


# Step 1: Create Folder Tests


def test_create_folder_success_without_parent() -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)
    fake_service.files_endpoint.queue(
        'create',
        _file_payload(
            file_id='folder_abc',
            name='My Reports',
            mime_type=DRIVE_FOLDER_MIME,
        ),
    )

    result = gateway.create_folder(name='My Reports', parent_id=None)

    assert isinstance(result, DriveMutationResult)
    assert result.file.file_id == 'folder_abc'
    assert result.file.name == 'My Reports'
    assert result.file.mime_type == DRIVE_FOLDER_MIME

    assert len(fake_service.files_endpoint.calls) == 1
    method, kwargs, request = fake_service.files_endpoint.calls[0]
    assert method == 'create'
    assert kwargs['body'] == {
        'name': 'My Reports',
        'mimeType': DRIVE_FOLDER_MIME,
    }
    assert kwargs['supportsAllDrives'] is True
    assert kwargs['fields'] == DRIVE_FILE_FIELDS
    assert request.retries == [2]


def test_create_folder_success_with_parent() -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)
    fake_service.files_endpoint.queue(
        'create',
        _file_payload(
            file_id='folder_child',
            name='Nested Folder',
            mime_type=DRIVE_FOLDER_MIME,
            parents=['parent_folder_1'],
        ),
    )

    result = gateway.create_folder(
        name='Nested Folder', parent_id='parent_folder_1'
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.file_id == 'folder_child'
    assert result.file.parents == ('parent_folder_1',)

    method, kwargs, _ = fake_service.files_endpoint.calls[0]
    assert kwargs['body'] == {
        'name': 'Nested Folder',
        'mimeType': DRIVE_FOLDER_MIME,
        'parents': ['parent_folder_1'],
    }


def test_create_folder_rejects_invalid_names() -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveInputError, match='[Ff]older name'):
        gateway.create_folder(name='')

    with pytest.raises(DriveInputError, match='[Ff]older name'):
        gateway.create_folder(name='   ')

    with pytest.raises(DriveInputError, match='[Ff]older name'):
        gateway.create_folder(name='a' * (MAX_DRIVE_NAME_CHARS + 1))

    assert len(fake_service.files_endpoint.calls) == 0


def test_create_folder_rejects_invalid_parent_id() -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveInputError, match='[Pp]arent'):
        gateway.create_folder(name='Valid', parent_id='')

    with pytest.raises(DriveInputError, match='[Pp]arent'):
        gateway.create_folder(name='Valid', parent_id='   ')

    with pytest.raises(DriveInputError, match='[Pp]arent'):
        gateway.create_folder(name='Valid', parent_id='p' * 300)

    assert len(fake_service.files_endpoint.calls) == 0


def test_create_folder_handles_provider_errors() -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=_http_error(401, 'unauthorized'))
    )
    with pytest.raises(
        DriveProviderError, match='Google authorization requires renewal'
    ):
        gateway.create_folder(name='Folder 401')

    fake_service.files_endpoint.queue(
        'create',
        FakeRequest(error=_http_error(403, 'insufficientFilePermissions')),
    )
    with pytest.raises(
        DriveScopeError,
        match='Drive operation requires additional permissions',
    ):
        gateway.create_folder(name='Folder 403')

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=_http_error(404, 'notFound'))
    )
    with pytest.raises(
        DriveProviderError, match='Drive resource was not found'
    ):
        gateway.create_folder(name='Folder 404')

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=_http_error(429, 'rateLimitExceeded'))
    )
    with pytest.raises(
        DriveProviderError, match='Drive is temporarily rate limited'
    ):
        gateway.create_folder(name='Folder 429')

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=TransportError('Network down'))
    )
    with pytest.raises(
        DriveProviderError, match='Drive request is temporarily unavailable'
    ):
        gateway.create_folder(name='Folder Net')


def test_create_folder_rejects_malformed_response() -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    fake_service.files_endpoint.queue(
        'create', {'name': 'Folder', 'mimeType': DRIVE_FOLDER_MIME}
    )
    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.create_folder(name='Folder')


# Step 2: Upload File Tests


def test_upload_file_success_without_parent(
    managed_store: ManagedFileStore,
) -> None:
    payload = b'Hello, Drive upload!'
    record = managed_store.publish_bytes(
        namespace='test',
        object_id='obj_1',
        original_name='hello.txt',
        mime_type='text/plain',
        expected_size=len(payload),
        data=payload,
    )

    fake_service = FakeDriveService()
    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: fake_service,
        uploader_factory=FakeUploader,
    )
    fake_service.files_endpoint.queue(
        'create',
        _file_payload(
            file_id='uploaded_123',
            name='hello.txt',
            mime_type='text/plain',
            size=len(payload),
        ),
    )

    result = gateway.upload_file(
        managed_name=record.managed_name,
        expected_size=record.size,
        expected_sha256=record.sha256,
        name='hello.txt',
        mime_type='text/plain',
        parent_id=None,
        files=managed_store,
    )

    assert isinstance(result, DriveMutationResult)
    assert result.file.file_id == 'uploaded_123'
    assert result.file.name == 'hello.txt'
    assert result.file.size == len(payload)

    assert len(fake_service.files_endpoint.calls) == 1
    method, kwargs, request = fake_service.files_endpoint.calls[0]
    assert method == 'create'
    assert kwargs['body'] == {'name': 'hello.txt'}
    assert kwargs['supportsAllDrives'] is True
    assert kwargs['fields'] == DRIVE_FILE_FIELDS
    assert request.retries == [2]

    assert len(FakeUploader.instances) == 1
    uploader = FakeUploader.instances[0]
    assert uploader.was_open_at_init is True
    assert uploader.mimetype == 'text/plain'
    assert uploader.resumable is False
    assert uploader.captured_content == payload
    assert kwargs['media_body'] is uploader


def test_upload_file_success_with_parent(
    managed_store: ManagedFileStore,
) -> None:
    payload = b'Some spreadsheet csv'
    record = managed_store.publish_bytes(
        namespace='test',
        object_id='obj_2',
        original_name='data.csv',
        mime_type='text/csv',
        expected_size=len(payload),
        data=payload,
    )

    fake_service = FakeDriveService()
    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: fake_service,
        uploader_factory=FakeUploader,
    )
    fake_service.files_endpoint.queue(
        'create',
        _file_payload(
            file_id='uploaded_456',
            name='data.csv',
            mime_type='text/csv',
            size=len(payload),
            parents=['folder_target_99'],
        ),
    )

    result = gateway.upload_file(
        managed_name=record.managed_name,
        expected_size=record.size,
        expected_sha256=record.sha256,
        name='data.csv',
        mime_type='text/csv',
        parent_id='folder_target_99',
        files=managed_store,
    )

    assert result.file.file_id == 'uploaded_456'
    assert result.file.parents == ('folder_target_99',)

    method, kwargs, _ = fake_service.files_endpoint.calls[0]
    assert kwargs['body'] == {
        'name': 'data.csv',
        'parents': ['folder_target_99'],
    }


def test_upload_file_verifies_descriptor_lifetime(
    managed_store: ManagedFileStore,
) -> None:
    payload = b'Descriptor lifetime test'
    record = managed_store.publish_bytes(
        namespace='test',
        object_id='obj_life',
        original_name='life.bin',
        mime_type='application/octet-stream',
        expected_size=len(payload),
        data=payload,
    )

    descriptor_was_open_during_execute = False

    class InspectingRequest(FakeRequest):
        """Inspect request execution."""

        def execute(self, *, num_retries: int = 0) -> Any:
            """Execute prepared resource."""
            nonlocal descriptor_was_open_during_execute
            assert len(FakeUploader.instances) == 1
            fd = FakeUploader.instances[0].fd
            descriptor_was_open_during_execute = not fd.closed
            return _file_payload(
                file_id='file_live',
                name='life.bin',
                mime_type='application/octet-stream',
                size=len(payload),
            )

    fake_service = FakeDriveService()
    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: fake_service,
        uploader_factory=FakeUploader,
    )
    fake_service.files_endpoint.queue('create', InspectingRequest())

    gateway.upload_file(
        managed_name=record.managed_name,
        expected_size=record.size,
        expected_sha256=record.sha256,
        name='life.bin',
        mime_type='application/octet-stream',
        parent_id=None,
        files=managed_store,
    )

    assert descriptor_was_open_during_execute is True
    # Step: Verify descriptor closure
    assert FakeUploader.instances[0].fd.closed is True


def test_upload_file_preflight_fails_on_traversal(
    managed_store: ManagedFileStore,
) -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    traversal_names = [
        '../secret.txt',
        '/etc/passwd',
        'sub/file.txt',
        '..',
        '.',
        'file\x00.txt',
    ]
    for bad_name in traversal_names:
        with pytest.raises(DriveManagedFileError):
            gateway.upload_file(
                managed_name=bad_name,
                expected_size=10,
                expected_sha256='a' * 64,
                name='target.txt',
                mime_type='text/plain',
                parent_id=None,
                files=managed_store,
            )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_preflight_fails_on_nonexistent_file(
    managed_store: ManagedFileStore,
) -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(
        DriveManagedFileError, match='not found or inaccessible'
    ):
        gateway.upload_file(
            managed_name='nonexistent_file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='target.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_preflight_fails_on_symlink(
    managed_store: ManagedFileStore,
    tmp_path: Path,
) -> None:
    target = tmp_path / 'real_file.txt'
    target.write_bytes(b'real content')
    os.chmod(target, 0o600)

    symlink_path = tmp_path / 'symlink.txt'
    symlink_path.symlink_to(target)

    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveManagedFileError):
        gateway.upload_file(
            managed_name='symlink.txt',
            expected_size=len(b'real content'),
            expected_sha256=hashlib.sha256(b'real content').hexdigest(),
            name='symlink.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_preflight_fails_on_size_mismatch(
    managed_store: ManagedFileStore,
) -> None:
    payload = b'exact-content'
    record = managed_store.publish_bytes(
        namespace='test',
        object_id='obj_size',
        original_name='size.txt',
        mime_type='text/plain',
        expected_size=len(payload),
        data=payload,
    )

    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveManagedFileError, match='size mismatch'):
        gateway.upload_file(
            managed_name=record.managed_name,
            expected_size=len(payload) + 5,
            expected_sha256=record.sha256,
            name='size.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_preflight_fails_on_hash_mismatch(
    managed_store: ManagedFileStore,
) -> None:
    payload = b'hash-check-data'
    record = managed_store.publish_bytes(
        namespace='test',
        object_id='obj_hash',
        original_name='hash.txt',
        mime_type='text/plain',
        expected_size=len(payload),
        data=payload,
    )

    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    wrong_sha = '0' * 64
    with pytest.raises(DriveManagedFileError, match='digest mismatch'):
        gateway.upload_file(
            managed_name=record.managed_name,
            expected_size=record.size,
            expected_sha256=wrong_sha,
            name='hash.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_rejects_oversized_expected_size(
    managed_store: ManagedFileStore,
) -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveInputError, match='25 MiB'):
        gateway.upload_file(
            managed_name='file.bin',
            expected_size=MAX_DRIVE_DOWNLOAD_BYTES + 1,
            expected_sha256='a' * 64,
            name='file.bin',
            mime_type='application/octet-stream',
            parent_id=None,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_rejects_negative_expected_size(
    managed_store: ManagedFileStore,
) -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveInputError, match='[Ss]ize'):
        gateway.upload_file(
            managed_name='file.bin',
            expected_size=-1,
            expected_sha256='a' * 64,
            name='file.bin',
            mime_type='application/octet-stream',
            parent_id=None,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_rejects_invalid_inputs(
    managed_store: ManagedFileStore,
) -> None:
    fake_service = FakeDriveService()
    gateway = DriveGateway(FakeStore(), service_builder=lambda _: fake_service)

    with pytest.raises(DriveInputError, match='[Nn]ame'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='[Nn]ame'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='   ',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='[Nn]ame'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='a' * (MAX_DRIVE_NAME_CHARS + 1),
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='MIME'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='file.txt',
            mime_type='',
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='MIME'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='file.txt',
            mime_type='m' * 256,
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='SHA-256'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='short',
            name='file.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='SHA-256'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='g' * 64,
            name='file.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='[Pp]arent'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='file.txt',
            mime_type='text/plain',
            parent_id='',
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='[Pp]arent'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='file.txt',
            mime_type='text/plain',
            parent_id='   ',
            files=managed_store,
        )

    with pytest.raises(DriveInputError, match='[Pp]arent'):
        gateway.upload_file(
            managed_name='file.txt',
            expected_size=10,
            expected_sha256='a' * 64,
            name='file.txt',
            mime_type='text/plain',
            parent_id='p' * 300,
            files=managed_store,
        )

    assert len(fake_service.files_endpoint.calls) == 0


def test_upload_file_handles_provider_errors(
    managed_store: ManagedFileStore,
) -> None:
    payload = b'Data for error tests'
    record = managed_store.publish_bytes(
        namespace='test',
        object_id='obj_err',
        original_name='error.txt',
        mime_type='text/plain',
        expected_size=len(payload),
        data=payload,
    )

    fake_service = FakeDriveService()
    gateway = DriveGateway(
        FakeStore(),
        service_builder=lambda _: fake_service,
        uploader_factory=FakeUploader,
    )

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=_http_error(401, 'auth'))
    )
    with pytest.raises(
        DriveProviderError, match='Google authorization requires renewal'
    ):
        gateway.upload_file(
            managed_name=record.managed_name,
            expected_size=record.size,
            expected_sha256=record.sha256,
            name='error.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    fake_service.files_endpoint.queue(
        'create',
        FakeRequest(error=_http_error(403, 'appNotAuthorizedToFile')),
    )
    with pytest.raises(
        DriveScopeError,
        match='Drive operation requires additional permissions',
    ):
        gateway.upload_file(
            managed_name=record.managed_name,
            expected_size=record.size,
            expected_sha256=record.sha256,
            name='error.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=_http_error(404, 'notFound'))
    )
    with pytest.raises(
        DriveProviderError, match='Drive resource was not found'
    ):
        gateway.upload_file(
            managed_name=record.managed_name,
            expected_size=record.size,
            expected_sha256=record.sha256,
            name='error.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )

    fake_service.files_endpoint.queue(
        'create', FakeRequest(error=TransportError('Timeout'))
    )
    with pytest.raises(
        DriveProviderError, match='Drive request is temporarily unavailable'
    ):
        gateway.upload_file(
            managed_name=record.managed_name,
            expected_size=record.size,
            expected_sha256=record.sha256,
            name='error.txt',
            mime_type='text/plain',
            parent_id=None,
            files=managed_store,
        )
