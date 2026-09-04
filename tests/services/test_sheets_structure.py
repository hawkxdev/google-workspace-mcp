"""Test Sheets structural operations."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

import httplib2  # type: ignore[import-untyped]
import pytest
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import GoogleCredentials
from google_workspace_mcp.services.sheets.client import SheetsGateway
from google_workspace_mcp.services.sheets.constants import (
    MAX_SHEETS_CELLS,
    MAX_SHEETS_GRID_CELLS,
    MAX_SHEETS_TITLE_CHARS,
)
from google_workspace_mcp.services.sheets.errors import (
    SheetsInputError,
    SheetsNotFoundError,
    SheetsProviderError,
    SheetsScopeError,
)
from google_workspace_mcp.services.sheets.schemas import (
    SheetCopyResult,
    SheetMutationResult,
    SheetSummary,
    SpreadsheetCreateResult,
)


class FakeRequest:
    """Record Sheets request execution."""

    def __init__(
        self, value: Any = None, error: Exception | None = None
    ) -> None:
        """Initialize Sheets request fake."""
        self.value = value
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0) -> Any:
        """Execute Sheets request fake."""
        self.retries.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.value


class FakeSheetsSubEndpoint:
    """Record endpoint calls."""

    def __init__(self) -> None:
        """Initialize fake endpoint state."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        """Queue fake endpoint values."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record fake endpoint request."""
        if not self.responses[method]:
            raise AssertionError(f'No queued response for {method}({kwargs})')
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def copyTo(self, **kwargs: Any) -> FakeRequest:
        """Record sheets copyTo call."""
        return self._call('copyTo', kwargs)


class FakeSpreadsheetsEndpoint:
    """Record Spreadsheets endpoint calls."""

    def __init__(self) -> None:
        """Initialize spreadsheets endpoint fake."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []
        self._sheets = FakeSheetsSubEndpoint()

    def queue(self, method: str, *values: Any) -> None:
        """Queue spreadsheets endpoint values."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record spreadsheets endpoint request."""
        if not self.responses[method]:
            raise AssertionError(f'No queued response for {method}({kwargs})')
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def create(self, **kwargs: Any) -> FakeRequest:
        """Record spreadsheets create call."""
        return self._call('create', kwargs)

    def batchUpdate(self, **kwargs: Any) -> FakeRequest:
        """Record spreadsheets batchUpdate call."""
        return self._call('batchUpdate', kwargs)

    def sheets(self) -> FakeSheetsSubEndpoint:
        """Return fake sheets endpoint."""
        return self._sheets


class FakeSheetsService:
    """Expose fake Sheets endpoints."""

    def __init__(self) -> None:
        """Initialize Sheets service fake."""
        self._spreadsheets = FakeSpreadsheetsEndpoint()

    def spreadsheets(self) -> FakeSpreadsheetsEndpoint:
        """Return spreadsheets endpoint."""
        return self._spreadsheets

    @property
    def last_batch_request(self) -> dict[str, Any] | None:
        """Return latest batch request."""
        if not self._spreadsheets.calls:
            return None
        for method, kwargs, _ in reversed(self._spreadsheets.calls):
            if method == 'batchUpdate':
                body = kwargs.get('body', {})
                requests = body.get('requests', [])
                if requests:
                    return requests[-1]
        return None


class FakeStore:
    """Return Sheets test credentials."""

    def __init__(self) -> None:
        """Initialize Sheets store fake."""
        self.calls = 0
        self.credentials = GoogleCredentials(
            token='test-token',
            refresh_token='test-refresh',
            client_id='test-client',
            client_secret='test-secret',
            scopes=('https://www.googleapis.com/auth/spreadsheets',),
        )

    def refresh(self) -> GoogleCredentials:
        """Record credential refresh."""
        self.calls += 1
        return self.credentials


def _make_http_error(status: int, reason: str | None = None) -> HttpError:
    """Build provider HTTP error."""
    resp = httplib2.Response({'status': str(status)})
    content_dict: dict[str, Any] = {'error': {'code': status}}
    if reason:
        content_dict['error']['errors'] = [{'reason': reason}]
    content = json.dumps(content_dict).encode('utf-8')
    return HttpError(
        resp,
        content,
        uri='https://sheets.googleapis.com/v4/spreadsheets/structure-test',
    )


@pytest.fixture
def fake_service() -> FakeSheetsService:
    """Provide fake Sheets service."""
    return FakeSheetsService()


@pytest.fixture
def store() -> FakeStore:
    """Provide fake credentials store."""
    return FakeStore()


@pytest.fixture
def gateway(
    store: FakeStore, fake_service: FakeSheetsService
) -> SheetsGateway:
    """Provide configured test gateway."""
    return SheetsGateway(store, service_builder=lambda _: fake_service)  # type: ignore[arg-type]


# === Rename Sheet Tests ===


def test_rename_uses_title_only_field_mask(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'updateSheetProperties': {
                        'properties': {
                            'sheetId': 42,
                            'title': 'Renamed',
                            'index': 1,
                            'sheetType': 'GRID',
                            'gridProperties': {
                                'rowCount': 100,
                                'columnCount': 26,
                            },
                        }
                    }
                }
            ]
        },
    )

    result = gateway.rename_sheet('book-1', 42, 'Renamed')
    request = fake_service.last_batch_request
    assert request == {
        'updateSheetProperties': {
            'properties': {'sheetId': 42, 'title': 'Renamed'},
            'fields': 'title',
        }
    }
    assert result == SheetMutationResult(
        spreadsheet_id='book-1',
        sheet_id=42,
        title='Renamed',
        index=1,
        sheet_type='GRID',
        row_count=100,
        column_count=26,
    )


@pytest.mark.parametrize(
    'spreadsheet_id',
    ['', '   ', '\x00', 'book\x001', 123, None],
)
def test_rename_sheet_validates_spreadsheet_id_before_refresh(
    store: FakeStore, gateway: SheetsGateway, spreadsheet_id: Any
) -> None:
    with pytest.raises(SheetsInputError, match='Spreadsheet ID is required'):
        gateway.rename_sheet(spreadsheet_id, 0, 'NewName')
    assert store.calls == 0


@pytest.mark.parametrize(
    'sheet_id',
    [-1, -100, True, False, '0', None, 1.5],
)
def test_rename_sheet_validates_sheet_id_before_refresh(
    store: FakeStore, gateway: SheetsGateway, sheet_id: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Sheet ID must be a non-negative integer'
    ):
        gateway.rename_sheet('book-1', sheet_id, 'NewName')
    assert store.calls == 0


@pytest.mark.parametrize(
    'new_title',
    [
        '',
        '   ',
        '\x00',
        'a' * (MAX_SHEETS_TITLE_CHARS + 1),
        123,
        None,
    ],
)
def test_rename_sheet_validates_title_before_refresh(
    store: FakeStore, gateway: SheetsGateway, new_title: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Sheet title must be 1-100 characters'
    ):
        gateway.rename_sheet('book-1', 0, new_title)
    assert store.calls == 0


def test_rename_sheet_handles_not_found(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate', FakeRequest(error=_make_http_error(404))
    )
    with pytest.raises(SheetsNotFoundError):
        gateway.rename_sheet('book-1', 0, 'NewTitle')


def test_rename_sheet_handles_malformed_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue('batchUpdate', {'replies': []})
    with pytest.raises(SheetsProviderError, match='invalid response'):
        gateway.rename_sheet('book-1', 0, 'NewTitle')


# === Create Spreadsheet Tests ===


def test_create_spreadsheet_root_only_with_locale_and_tz(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'create',
        {
            'spreadsheetId': 'created-123',
            'properties': {
                'title': 'New Book',
                'locale': 'en_US',
                'timeZone': 'America/New_York',
            },
            'spreadsheetUrl': 'https://docs.google.com/spreadsheets/d/created-123/edit',
            'sheets': [
                {
                    'properties': {
                        'sheetId': 0,
                        'title': 'Sheet1',
                        'index': 0,
                        'sheetType': 'GRID',
                        'gridProperties': {
                            'rowCount': 1000,
                            'columnCount': 26,
                        },
                    }
                }
            ],
        },
    )

    result = gateway.create_spreadsheet(
        'New Book', locale='en_US', time_zone='America/New_York'
    )
    _, kwargs, _ = fake_service.spreadsheets().calls[-1]
    assert kwargs['body'] == {
        'properties': {
            'title': 'New Book',
            'locale': 'en_US',
            'timeZone': 'America/New_York',
        }
    }
    assert result == SpreadsheetCreateResult(
        spreadsheet_id='created-123',
        title='New Book',
        locale='en_US',
        time_zone='America/New_York',
        url='https://docs.google.com/spreadsheets/d/created-123/edit',
        sheets=(
            SheetSummary(
                sheet_id=0,
                title='Sheet1',
                index=0,
                sheet_type='GRID',
                row_count=1000,
                column_count=26,
            ),
        ),
    )


def test_create_spreadsheet_minimal(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'create',
        {
            'spreadsheetId': 'created-456',
            'properties': {'title': 'Simple Book'},
            'sheets': [],
        },
    )

    result = gateway.create_spreadsheet('Simple Book')
    _, kwargs, _ = fake_service.spreadsheets().calls[-1]
    assert kwargs['body'] == {'properties': {'title': 'Simple Book'}}
    assert result.spreadsheet_id == 'created-456'
    assert result.title == 'Simple Book'
    assert result.locale is None
    assert result.time_zone is None
    assert result.sheets == ()


@pytest.mark.parametrize(
    'title',
    ['', '   ', '\x00', 'a' * (MAX_SHEETS_TITLE_CHARS + 1), 123, None],
)
def test_create_spreadsheet_validates_title_before_refresh(
    store: FakeStore, gateway: SheetsGateway, title: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Spreadsheet title must be 1-100 characters'
    ):
        gateway.create_spreadsheet(title)
    assert store.calls == 0


@pytest.mark.parametrize(
    'locale',
    ['', '   ', '\x00', 'a' * (MAX_SHEETS_TITLE_CHARS + 1), 123],
)
def test_create_spreadsheet_validates_locale_before_refresh(
    store: FakeStore, gateway: SheetsGateway, locale: Any
) -> None:
    with pytest.raises(SheetsInputError, match='Invalid locale'):
        gateway.create_spreadsheet('Valid Title', locale=locale)
    assert store.calls == 0


@pytest.mark.parametrize(
    'time_zone',
    ['', '   ', '\x00', 'a' * (MAX_SHEETS_TITLE_CHARS + 1), 123],
)
def test_create_spreadsheet_validates_time_zone_before_refresh(
    store: FakeStore, gateway: SheetsGateway, time_zone: Any
) -> None:
    with pytest.raises(SheetsInputError, match='Invalid time zone'):
        gateway.create_spreadsheet('Valid Title', time_zone=time_zone)
    assert store.calls == 0


def test_create_spreadsheet_handles_provider_errors(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'create',
        FakeRequest(
            error=_make_http_error(403, 'ACCESS_TOKEN_SCOPE_INSUFFICIENT')
        ),
    )
    with pytest.raises(SheetsScopeError):
        gateway.create_spreadsheet('New Book')


def test_create_spreadsheet_handles_malformed_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue('create', {'properties': {'title': 'T'}})
    with pytest.raises(SheetsProviderError, match='invalid response'):
        gateway.create_spreadsheet('New Book')


# === Add Sheet Tests ===


def test_add_sheet_success_with_dimensions_and_index(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'addSheet': {
                        'properties': {
                            'sheetId': 101,
                            'title': 'CustomSheet',
                            'index': 3,
                            'sheetType': 'GRID',
                            'gridProperties': {
                                'rowCount': 500,
                                'columnCount': 10,
                            },
                        }
                    }
                }
            ]
        },
    )

    result = gateway.add_sheet(
        'book-1', 'CustomSheet', row_count=500, column_count=10, index=3
    )
    assert fake_service.last_batch_request == {
        'addSheet': {
            'properties': {
                'title': 'CustomSheet',
                'gridProperties': {'rowCount': 500, 'columnCount': 10},
                'index': 3,
            }
        }
    }
    assert result == SheetMutationResult(
        spreadsheet_id='book-1',
        sheet_id=101,
        title='CustomSheet',
        index=3,
        sheet_type='GRID',
        row_count=500,
        column_count=10,
    )


def test_add_sheet_minimal_arguments(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'addSheet': {
                        'properties': {
                            'sheetId': 102,
                            'title': 'SimpleSheet',
                            'index': 1,
                            'sheetType': 'GRID',
                        }
                    }
                }
            ]
        },
    )

    result = gateway.add_sheet('book-1', 'SimpleSheet')
    assert fake_service.last_batch_request == {
        'addSheet': {
            'properties': {
                'title': 'SimpleSheet',
            }
        }
    }
    assert result.sheet_id == 102
    assert result.title == 'SimpleSheet'
    assert result.row_count is None
    assert result.column_count is None


@pytest.mark.parametrize(
    'spreadsheet_id',
    ['', '   ', '\x00', 123, None],
)
def test_add_sheet_validates_spreadsheet_id_before_refresh(
    store: FakeStore, gateway: SheetsGateway, spreadsheet_id: Any
) -> None:
    with pytest.raises(SheetsInputError, match='Spreadsheet ID is required'):
        gateway.add_sheet(spreadsheet_id, 'Sheet1')
    assert store.calls == 0


@pytest.mark.parametrize(
    'title',
    ['', '   ', '\x00', 'a' * (MAX_SHEETS_TITLE_CHARS + 1), 123, None],
)
def test_add_sheet_validates_title_before_refresh(
    store: FakeStore, gateway: SheetsGateway, title: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Sheet title must be 1-100 characters'
    ):
        gateway.add_sheet('book-1', title)
    assert store.calls == 0


def test_add_sheet_rejects_single_dimension_before_refresh(
    store: FakeStore, gateway: SheetsGateway
) -> None:
    with pytest.raises(
        SheetsInputError,
        match='row_count and column_count must be provided together',
    ):
        gateway.add_sheet('book-1', 'Sheet1', row_count=100)
    assert store.calls == 0

    with pytest.raises(
        SheetsInputError,
        match='row_count and column_count must be provided together',
    ):
        gateway.add_sheet('book-1', 'Sheet1', column_count=20)
    assert store.calls == 0


def test_add_sheet_grid_cells_product_limit_before_refresh(
    store: FakeStore, gateway: SheetsGateway
) -> None:
    with pytest.raises(
        SheetsInputError,
        match=f'Total grid cells cannot exceed {MAX_SHEETS_GRID_CELLS}',
    ):
        gateway.add_sheet(
            'book-1', 'Sheet1', row_count=10_000, column_count=1_001
        )
    assert store.calls == 0


def test_add_sheet_grid_cells_valid_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'addSheet': {
                        'properties': {
                            'sheetId': 105,
                            'title': 'MaxGrid',
                            'index': 0,
                            'sheetType': 'GRID',
                            'gridProperties': {
                                'rowCount': 10_000,
                                'columnCount': 1_000,
                            },
                        }
                    }
                }
            ]
        },
    )

    result = gateway.add_sheet(
        'book-1', 'MaxGrid', row_count=10_000, column_count=1_000
    )
    assert fake_service.last_batch_request == {
        'addSheet': {
            'properties': {
                'title': 'MaxGrid',
                'gridProperties': {'rowCount': 10_000, 'columnCount': 1_000},
            }
        }
    }
    assert result.row_count == 10_000
    assert result.column_count == 1_000


@pytest.mark.parametrize(
    'row_count',
    [0, -1, MAX_SHEETS_GRID_CELLS + 1, True, False, 1.5, '100'],
)
def test_add_sheet_validates_row_count_before_refresh(
    store: FakeStore, gateway: SheetsGateway, row_count: Any
) -> None:
    with pytest.raises(
        SheetsInputError,
        match=f'row_count must be between 1 and {MAX_SHEETS_GRID_CELLS}',
    ):
        gateway.add_sheet(
            'book-1', 'ValidTitle', row_count=row_count, column_count=10
        )
    assert store.calls == 0


@pytest.mark.parametrize(
    'column_count',
    [0, -1, MAX_SHEETS_GRID_CELLS + 1, True, False, 1.5, '20'],
)
def test_add_sheet_validates_column_count_before_refresh(
    store: FakeStore, gateway: SheetsGateway, column_count: Any
) -> None:
    with pytest.raises(
        SheetsInputError,
        match=f'column_count must be between 1 and {MAX_SHEETS_GRID_CELLS}',
    ):
        gateway.add_sheet(
            'book-1', 'ValidTitle', row_count=100, column_count=column_count
        )
    assert store.calls == 0


@pytest.mark.parametrize(
    'index',
    [-1, MAX_SHEETS_CELLS + 1, True, False, 1.5, '0'],
)
def test_add_sheet_validates_index_before_refresh(
    store: FakeStore, gateway: SheetsGateway, index: Any
) -> None:
    with pytest.raises(
        SheetsInputError,
        match=f'index must be between 0 and {MAX_SHEETS_CELLS}',
    ):
        gateway.add_sheet('book-1', 'ValidTitle', index=index)
    assert store.calls == 0


def test_add_sheet_handles_malformed_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {'replies': [{'addSheet': {}}]},
    )
    with pytest.raises(SheetsProviderError, match='invalid response'):
        gateway.add_sheet('book-1', 'Sheet1')


# === Copy Sheet Tests ===


def test_copy_sheet_same_book_with_title(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'duplicateSheet': {
                        'properties': {
                            'sheetId': 201,
                            'title': 'CopiedSheet',
                            'index': 2,
                            'sheetType': 'GRID',
                            'gridProperties': {
                                'rowCount': 200,
                                'columnCount': 15,
                            },
                        }
                    }
                }
            ]
        },
    )

    result = gateway.copy_sheet(
        'book-1', 42, 'book-1', new_title='CopiedSheet'
    )
    assert fake_service.last_batch_request == {
        'duplicateSheet': {
            'sourceSheetId': 42,
            'newSheetName': 'CopiedSheet',
        }
    }
    assert result == SheetCopyResult(
        source_spreadsheet_id='book-1',
        destination_spreadsheet_id='book-1',
        sheet_id=201,
        title='CopiedSheet',
        index=2,
        sheet_type='GRID',
        row_count=200,
        column_count=15,
    )


def test_copy_sheet_same_book_without_title(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'duplicateSheet': {
                        'properties': {
                            'sheetId': 202,
                            'title': 'Copy of Sheet1',
                            'index': 1,
                            'sheetType': 'GRID',
                        }
                    }
                }
            ]
        },
    )

    result = gateway.copy_sheet('book-1', 42, 'book-1')
    assert fake_service.last_batch_request == {
        'duplicateSheet': {
            'sourceSheetId': 42,
        }
    }
    assert result.sheet_id == 202
    assert result.title == 'Copy of Sheet1'


def test_copy_sheet_cross_book_success(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().sheets().queue(
        'copyTo',
        {
            'sheetId': 301,
            'title': 'Copy of SourceSheet',
            'index': 0,
            'sheetType': 'GRID',
            'gridProperties': {'rowCount': 100, 'columnCount': 26},
        },
    )

    result = gateway.copy_sheet('source-book', 10, 'dest-book')
    calls = fake_service.spreadsheets().sheets().calls
    assert len(calls) == 1
    method, kwargs, request = calls[0]
    assert method == 'copyTo'
    assert kwargs == {
        'spreadsheetId': 'source-book',
        'sheetId': 10,
        'body': {'destinationSpreadsheetId': 'dest-book'},
    }
    assert request.retries == [0]

    assert len(fake_service.spreadsheets().calls) == 0

    assert result == SheetCopyResult(
        source_spreadsheet_id='source-book',
        destination_spreadsheet_id='dest-book',
        sheet_id=301,
        title='Copy of SourceSheet',
        index=0,
        sheet_type='GRID',
        row_count=100,
        column_count=26,
    )


def test_copy_sheet_cross_book_rejects_new_title_before_refresh(
    store: FakeStore, gateway: SheetsGateway
) -> None:
    with pytest.raises(
        SheetsInputError,
        match='new_title is only supported for same-spreadsheet copy',
    ):
        gateway.copy_sheet(
            'source-book', 10, 'dest-book', new_title='ForbiddenName'
        )
    assert store.calls == 0


@pytest.mark.parametrize(
    'source_id',
    ['', '   ', '\x00', 123, None],
)
def test_copy_sheet_validates_source_id_before_refresh(
    store: FakeStore, gateway: SheetsGateway, source_id: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Source spreadsheet ID is required'
    ):
        gateway.copy_sheet(source_id, 0, 'dest-book')
    assert store.calls == 0


@pytest.mark.parametrize(
    'dest_id',
    ['', '   ', '\x00', 123, None],
)
def test_copy_sheet_validates_dest_id_before_refresh(
    store: FakeStore, gateway: SheetsGateway, dest_id: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Destination spreadsheet ID is required'
    ):
        gateway.copy_sheet('source-book', 0, dest_id)
    assert store.calls == 0


@pytest.mark.parametrize(
    'sheet_id',
    [-1, True, False, '0', None, 1.5],
)
def test_copy_sheet_validates_sheet_id_before_refresh(
    store: FakeStore, gateway: SheetsGateway, sheet_id: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Sheet ID must be a non-negative integer'
    ):
        gateway.copy_sheet('source-book', sheet_id, 'dest-book')
    assert store.calls == 0


@pytest.mark.parametrize(
    'new_title',
    ['', '   ', '\x00', 'a' * (MAX_SHEETS_TITLE_CHARS + 1), 123],
)
def test_copy_sheet_validates_new_title_before_refresh(
    store: FakeStore, gateway: SheetsGateway, new_title: Any
) -> None:
    with pytest.raises(
        SheetsInputError, match='Sheet title must be 1-100 characters'
    ):
        gateway.copy_sheet(
            'source-book', 0, 'source-book', new_title=new_title
        )
    assert store.calls == 0


def test_copy_sheet_handles_malformed_same_book_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {'replies': [{'duplicateSheet': {}}]},
    )
    with pytest.raises(SheetsProviderError, match='invalid response'):
        gateway.copy_sheet('book-1', 0, 'book-1')


def test_copy_sheet_handles_malformed_cross_book_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().sheets().queue('copyTo', {})
    with pytest.raises(SheetsProviderError, match='invalid response'):
        gateway.copy_sheet('source-book', 0, 'dest-book')


# === Write num_retries=0 Verification ===


def test_all_structural_write_methods_use_zero_retries(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().queue(
        'create',
        {
            'spreadsheetId': 'c1',
            'properties': {'title': 'T'},
            'sheets': [],
        },
    )
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'addSheet': {
                        'properties': {'sheetId': 1, 'title': 'S1', 'index': 0}
                    }
                }
            ]
        },
    )
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'updateSheetProperties': {
                        'properties': {'sheetId': 1, 'title': 'S2', 'index': 0}
                    }
                }
            ]
        },
    )
    fake_service.spreadsheets().queue(
        'batchUpdate',
        {
            'replies': [
                {
                    'duplicateSheet': {
                        'properties': {
                            'sheetId': 2,
                            'title': 'S2 Copy',
                            'index': 1,
                        }
                    }
                }
            ]
        },
    )
    fake_service.spreadsheets().sheets().queue(
        'copyTo',
        {'sheetId': 3, 'title': 'S3', 'index': 0},
    )

    gateway.create_spreadsheet('T')
    gateway.add_sheet('c1', 'S1')
    gateway.rename_sheet('c1', 1, 'S2')
    gateway.copy_sheet('c1', 1, 'c1')
    gateway.copy_sheet('c1', 1, 'c2')

    for _, _, req in fake_service.spreadsheets().calls:
        assert req.retries == [0]
    for _, _, req in fake_service.spreadsheets().sheets().calls:
        assert req.retries == [0]
