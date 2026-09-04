"""Test Drive provider gateway."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

import httplib2  # type: ignore[import-untyped]
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.services.drive.client import (
    DriveGateway,
)
from google_workspace_mcp.services.drive.constants import (
    DRIVE_FILE_FIELDS,
    DRIVE_LIST_FIELDS,
    MAX_DRIVE_FILES,
    MAX_DRIVE_PAGE_SIZE,
    MAX_DRIVE_PARENTS,
)
from google_workspace_mcp.services.drive.errors import (
    DriveConflictError,
    DriveProviderError,
    DriveScopeError,
)
from google_workspace_mcp.services.drive.schemas import (
    DriveFile,
    DriveFileList,
    DriveSearchFilters,
)


class FakeRequest:
    """Record Drive request execution."""

    def __init__(
        self, value: Any = None, error: Exception | None = None
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

    def list(self, **kwargs: Any) -> FakeRequest:
        """List fake list."""
        return self._call('list', kwargs)

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

    def __init__(self, fail: bool = False) -> None:
        """Initialize test double."""
        self.calls = 0
        self.fail = fail
        self.service_name = 'drive'
        self.oauth_state = None  # type: ignore[assignment]
        self.credentials = GoogleCredentials(
            token='drive-test-token',
            scopes=(
                'https://www.googleapis.com/auth/drive.readonly',
                'https://www.googleapis.com/auth/drive.file',
            ),
        )

    def refresh(self, request: Any = None) -> GoogleCredentials:
        """Refresh fake resource."""
        self.calls += 1
        if self.fail:
            raise RuntimeError('Store refresh failed')
        return self.credentials


def _valid_file_payload(file_id: str = 'file_123') -> dict[str, Any]:
    """Build valid file payload."""
    return {
        'id': file_id,
        'name': 'Test Document.docx',
        'mimeType': (
            'application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document'
        ),
        'size': '1048576',
        'createdTime': '2026-08-25T10:00:00.000Z',
        'modifiedTime': '2026-08-25T12:00:00.000Z',
        'version': 3,
        'parents': ['folder_abc'],
        'webViewLink': 'https://drive.google.com/file/d/file_123/view',
        'md5Checksum': '0123456789abcdef0123456789abcdef',
        'sha1Checksum': '0123456789abcdef0123456789abcdef01234567',
        'sha256Checksum': (
            '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        ),
        'trashed': False,
        'shared': True,
        'driveId': 'drive_xyz',
    }


def _make_http_error(
    status: int, reason: str | None = None, message: str = 'Error'
) -> HttpError:
    """Build HTTP error."""
    body: dict[str, Any] = {
        'error': {
            'code': status,
            'message': message,
        }
    }
    if reason is not None:
        body['error']['errors'] = [{'reason': reason, 'message': message}]
    content = json.dumps(body).encode('utf-8')
    resp = httplib2.Response({'status': str(status)})
    return HttpError(resp, content)


def test_gateway_initialization_and_service_builder() -> None:
    store = FakeStore()
    custom_service = FakeDriveService()
    builder_called = False

    def custom_builder(creds: GoogleCredentials) -> Any:
        """Build custom service."""
        nonlocal builder_called
        builder_called = True
        assert creds.token == 'drive-test-token'
        return custom_service

    gateway = DriveGateway(
        store, service_builder=custom_builder, num_retries=3
    )
    svc = gateway.service()
    assert svc is custom_service
    assert builder_called is True
    assert store.calls == 1


def test_gateway_service_credential_failure() -> None:
    store = FakeStore(fail=True)
    gateway = DriveGateway(store)
    with pytest.raises(
        DriveProviderError, match='Drive credentials are unavailable'
    ):
        gateway.service()


def test_search_files_user_corpus_exact_kwargs() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    service.files_endpoint.queue(
        'list',
        {
            'files': [payload],
            'nextPageToken': 'next_token_123',
            'incompleteSearch': False,
        },
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)
    filters = DriveSearchFilters(
        text='quarterly report',
        exact_name='Q3.pdf',
        parent_id='parent_dir',
        mime_types=('application/pdf',),
    )

    result = gateway.search_files(
        filters, page_size=20, page_token='token_abc'
    )

    assert isinstance(result, DriveFileList)
    assert len(result.files) == 1
    assert result.files[0].file_id == 'file_123'
    assert result.files[0].name == 'Test Document.docx'
    assert result.files[0].size == 1048576
    assert result.files[0].version == 3
    assert result.files[0].parents == ('folder_abc',)
    assert result.files[0].shared is True
    assert result.next_page_token == 'next_token_123'
    assert result.incomplete_search is False

    method, kwargs, req = service.files_endpoint.calls[0]
    assert method == 'list'
    assert kwargs == {
        'q': (
            "'parent_dir' in parents and "
            "name = 'Q3.pdf' and "
            "(name contains 'quarterly report' or "
            "fullText contains 'quarterly report') and "
            "mimeType = 'application/pdf' and "
            'trashed = false'
        ),
        'spaces': 'drive',
        'corpora': 'user',
        'supportsAllDrives': True,
        'includeItemsFromAllDrives': True,
        'fields': DRIVE_LIST_FIELDS,
        'pageSize': 20,
        'pageToken': 'token_abc',
    }
    assert req.retries == [2]


def test_search_files_shared_drive_corpus_exact_kwargs() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'list',
        {
            'files': [],
            'nextPageToken': '',
            'incompleteSearch': True,
        },
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)
    filters = DriveSearchFilters(drive_id='shared_drive_999')

    result = gateway.search_files(filters, page_size=50, page_token=None)

    assert result.files == ()
    assert result.next_page_token == ''
    assert result.incomplete_search is True

    method, kwargs, req = service.files_endpoint.calls[0]
    assert method == 'list'
    assert kwargs == {
        'q': 'trashed = false',
        'spaces': 'drive',
        'corpora': 'drive',
        'driveId': 'shared_drive_999',
        'supportsAllDrives': True,
        'includeItemsFromAllDrives': True,
        'fields': DRIVE_LIST_FIELDS,
        'pageSize': 50,
    }
    assert 'pageToken' not in kwargs


def test_search_files_page_size_bounding() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('list', {'files': []})
    service.files_endpoint.queue('list', {'files': []})
    gateway = DriveGateway(store, service_builder=lambda _: service)

    gateway.search_files(DriveSearchFilters(), page_size=100)
    assert (
        service.files_endpoint.calls[0][1]['pageSize'] == MAX_DRIVE_PAGE_SIZE
    )

    gateway.search_files(DriveSearchFilters(), page_size=0)
    assert service.files_endpoint.calls[1][1]['pageSize'] == 1


def test_get_file_exact_kwargs() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload('target_file_id')
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    file_meta = gateway.get_file('target_file_id')

    assert isinstance(file_meta, DriveFile)
    assert file_meta.file_id == 'target_file_id'
    assert file_meta.name == 'Test Document.docx'
    assert file_meta.drive_id == 'drive_xyz'

    method, kwargs, req = service.files_endpoint.calls[0]
    assert method == 'get'
    assert kwargs == {
        'fileId': 'target_file_id',
        'supportsAllDrives': True,
        'fields': DRIVE_FILE_FIELDS,
    }
    assert req.retries == [2]


def test_list_folder_user_corpus_exact_kwargs() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'list',
        {
            'files': [_valid_file_payload('child_1')],
            'nextPageToken': 'page2',
        },
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    result = gateway.list_folder(
        'parent_dir_1', page_size=15, page_token='page1'
    )

    assert len(result.files) == 1
    assert result.files[0].file_id == 'child_1'
    assert result.next_page_token == 'page2'

    method, kwargs, req = service.files_endpoint.calls[0]
    assert method == 'list'
    assert kwargs == {
        'q': "'parent_dir_1' in parents and trashed = false",
        'spaces': 'drive',
        'corpora': 'user',
        'supportsAllDrives': True,
        'includeItemsFromAllDrives': True,
        'fields': DRIVE_LIST_FIELDS,
        'pageSize': 15,
        'pageToken': 'page1',
    }


def test_list_folder_shared_drive_exact_kwargs() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('list', {'files': []})
    gateway = DriveGateway(store, service_builder=lambda _: service)

    gateway.list_folder(
        'folder_in_shared_drive',
        page_size=25,
        page_token=None,
        drive_id='shared_drive_456',
    )

    method, kwargs, req = service.files_endpoint.calls[0]
    assert method == 'list'
    assert kwargs == {
        'q': "'folder_in_shared_drive' in parents and trashed = false",
        'spaces': 'drive',
        'corpora': 'drive',
        'driveId': 'shared_drive_456',
        'supportsAllDrives': True,
        'includeItemsFromAllDrives': True,
        'fields': DRIVE_LIST_FIELDS,
        'pageSize': 25,
    }


def test_missing_collection_keys_normalize_gracefully() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('list', {})
    gateway = DriveGateway(store, service_builder=lambda _: service)

    result = gateway.search_files(DriveSearchFilters(), page_size=10)
    assert result.files == ()
    assert result.next_page_token == ''
    assert result.incomplete_search is False


def test_file_metadata_without_optional_fields() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        {
            'id': 'minimal_id',
            'name': 'Untitled',
            'mimeType': 'application/vnd.google-apps.document',
        },
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    file_meta = gateway.get_file('minimal_id')
    assert file_meta.file_id == 'minimal_id'
    assert file_meta.name == 'Untitled'
    assert file_meta.mime_type == 'application/vnd.google-apps.document'
    assert file_meta.size is None
    assert file_meta.created_time == ''
    assert file_meta.modified_time == ''
    assert file_meta.version == 0
    assert file_meta.parents == ()
    assert file_meta.web_view_link == ''
    assert file_meta.md5_checksum == ''
    assert file_meta.sha1_checksum == ''
    assert file_meta.sha256_checksum == ''
    assert file_meta.trashed is False
    assert file_meta.shared is False
    assert file_meta.drive_id is None


def test_strict_parsing_non_mapping_response() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('get', 'not-a-mapping')
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_missing_required_file_id() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get', {'name': 'Doc', 'mimeType': 'text/plain'}
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_missing_required_name() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get', {'id': '123', 'mimeType': 'text/plain'}
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_missing_required_mime_type() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('get', {'id': '123', 'name': 'Doc'})
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_non_string_text_field() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['name'] = 12345
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_negative_size() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['size'] = -50
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_malformed_string_size() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['size'] = 'not-a-number'
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_negative_version() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['version'] = -1
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_oversized_parents() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['parents'] = ['p'] * (MAX_DRIVE_PARENTS + 1)
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_non_string_parent_item() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['parents'] = ['p1', 999]
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_strict_parsing_oversized_files_collection() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'list',
        {
            'files': [
                _valid_file_payload(f'f_{i}')
                for i in range(MAX_DRIVE_FILES + 1)
            ]
        },
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.search_files(DriveSearchFilters(), page_size=50)


def test_strict_parsing_non_bool_trashed() -> None:
    service = FakeDriveService()
    store = FakeStore()
    payload = _valid_file_payload()
    payload['trashed'] = 'true'
    service.files_endpoint.queue('get', payload)
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.get_file('file_id')


def test_error_taxonomy_401_authorization_renewal() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(
            error=_make_http_error(401, 'authError', 'Invalid credentials')
        ),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Google authorization requires renewal'
    ):
        gateway.get_file('f1')


@pytest.mark.parametrize(
    'reason',
    [
        'insufficientFilePermissions',
        'insufficientPermissions',
        'appNotAuthorizedToFile',
        'cannotModifyInheritedAccess',
        'domainPolicy',
    ],
)
def test_error_taxonomy_403_insufficient_permissions_maps_to_scope_error(
    reason: str,
) -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(error=_make_http_error(403, reason, 'Permission denied')),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveScopeError,
        match='Drive operation requires additional permissions',
    ):
        gateway.get_file('f1')


@pytest.mark.parametrize(
    'status,reason',
    [
        (403, 'rateLimitExceeded'),
        (403, 'userRateLimitExceeded'),
        (403, 'dailyLimitExceeded'),
        (429, 'rateLimitExceeded'),
        (429, None),
    ],
)
def test_error_taxonomy_rate_limits(status: int, reason: str | None) -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(
            error=_make_http_error(status, reason, 'Too many requests')
        ),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive is temporarily rate limited'
    ):
        gateway.get_file('f1')


def test_error_taxonomy_404_not_found() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(error=_make_http_error(404, 'notFound', 'File not found')),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive resource was not found'
    ):
        gateway.get_file('missing_file')


def test_error_taxonomy_409_conflict() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(error=_make_http_error(409, 'conflict', 'Conflict')),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveConflictError, match='Drive request conflicts with existing data'
    ):
        gateway.get_file('f1')


def test_error_taxonomy_412_precondition_failed() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(
            error=_make_http_error(
                412, 'conditionNotMet', 'Precondition failed'
            )
        ),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveConflictError, match='Drive file changed since it was read'
    ):
        gateway.get_file('f1')


def test_error_taxonomy_400_bad_request() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(error=_make_http_error(400, 'invalid', 'Bad request')),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(DriveProviderError, match='Drive rejected the request'):
        gateway.get_file('f1')


def test_error_taxonomy_generic_500_error() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue(
        'get',
        FakeRequest(
            error=_make_http_error(500, 'backendError', 'Internal error')
        ),
    )
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive request is temporarily unavailable'
    ):
        gateway.get_file('f1')


@pytest.mark.parametrize(
    'transport_exc',
    [
        TransportError('Failed to connect'),
        TimeoutError('Socket timeout'),
        ConnectionError('Connection refused'),
        OSError('Network unreachable'),
    ],
)
def test_error_taxonomy_transport_failures(transport_exc: Exception) -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('get', FakeRequest(error=transport_exc))
    gateway = DriveGateway(store, service_builder=lambda _: service)

    with pytest.raises(
        DriveProviderError, match='Drive request is temporarily unavailable'
    ):
        gateway.get_file('f1')


def test_gateway_execute_returns_mapping() -> None:
    service = FakeDriveService()
    store = FakeStore()
    service.files_endpoint.queue('get', {'id': '123', 'name': 'test'})
    gateway = DriveGateway(store, service_builder=lambda _: service)
    result = gateway.execute(FakeRequest({'id': '123', 'name': 'test'}))
    assert result == {'id': '123', 'name': 'test'}


def test_gateway_execute_rejects_non_mapping() -> None:
    service = FakeDriveService()
    store = FakeStore()
    gateway = DriveGateway(store, service_builder=lambda _: service)
    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        gateway.execute(FakeRequest(['not', 'a', 'dict']))


def test_safe_reason_helper() -> None:
    from google_workspace_mcp.services.drive.client import _safe_reason

    assert (
        _safe_reason('insufficientFilePermissions')
        == 'insufficientFilePermissions'
    )
    assert _safe_reason('rateLimitExceeded') == 'rateLimitExceeded'
    assert _safe_reason('notFound') == 'notFound'
    assert _safe_reason('unknownReason') == 'unknown'
    assert _safe_reason(None) == 'unknown'
    assert _safe_reason(123) == 'unknown'
    assert _safe_reason('x' * 200) == 'unknown'


def test_integer_helper() -> None:
    from google_workspace_mcp.services.drive.client import _integer

    assert _integer(10) == 10
    assert _integer('1048576') == 1048576
    assert _integer(0) == 0

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _integer(True)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _integer(False)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _integer('not_an_int')

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _integer(-1, min_val=0)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _integer(100, max_val=50)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _integer(3.14)


def test_sequence_helper() -> None:
    from google_workspace_mcp.services.drive.client import _sequence

    assert _sequence([1, 2, 3], limit=5) == [1, 2, 3]
    assert _sequence((1, 2), limit=2) == (1, 2)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _sequence('string_is_sequence_in_python_but_rejected', limit=100)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _sequence(b'bytes_rejected', limit=100)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _sequence([1, 2, 3], limit=2)


def test_text_and_optional_text_helpers() -> None:
    from google_workspace_mcp.services.drive.client import (
        _optional_text,
        _text,
    )

    assert _text('hello', limit=10) == 'hello'
    assert _text('', limit=10) == ''
    assert _optional_text(None) is None
    assert _optional_text('hello') == 'hello'

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _text(None)  # type: ignore[arg-type]

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _text(123)  # type: ignore[arg-type]

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _text('toolong', limit=3)

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _optional_text(123)  # type: ignore[arg-type]

    with pytest.raises(
        DriveProviderError, match='Drive returned an invalid response'
    ):
        _optional_text('toolong', limit=3)
