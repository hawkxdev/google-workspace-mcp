"""Test Sheets batch operations."""

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
    MAX_SHEETS_RANGES,
)
from google_workspace_mcp.services.sheets.errors import (
    SheetsInputError,
    SheetsNotFoundError,
    SheetsProviderError,
)
from google_workspace_mcp.services.sheets.schemas import (
    SheetsBatchWriteResult,
    SheetsClearResult,
    SheetsInputMode,
    SheetsWriteRange,
    SheetsWriteResult,
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


class FakeValuesEndpoint:
    """Record values endpoint calls."""

    def __init__(self) -> None:
        """Initialize values endpoint fake."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        """Queue values endpoint values."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record values endpoint request."""
        if not self.responses[method]:
            raise AssertionError(f'No queued response for {method}({kwargs})')
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def get(self, **kwargs: Any) -> FakeRequest:
        """Record values get call."""
        return self._call('get', kwargs)

    def batchGet(self, **kwargs: Any) -> FakeRequest:
        """Record values batchGet call."""
        return self._call('batchGet', kwargs)

    def update(self, **kwargs: Any) -> FakeRequest:
        """Record values update call."""
        return self._call('update', kwargs)

    def append(self, **kwargs: Any) -> FakeRequest:
        """Record values append call."""
        return self._call('append', kwargs)

    def batchUpdate(self, **kwargs: Any) -> FakeRequest:
        """Record values batchUpdate call."""
        return self._call('batchUpdate', kwargs)

    def batchClear(self, **kwargs: Any) -> FakeRequest:
        """Record values batchClear call."""
        return self._call('batchClear', kwargs)


class FakeSpreadsheetsEndpoint:
    """Record Spreadsheets endpoint calls."""

    def __init__(self) -> None:
        """Initialize spreadsheets endpoint fake."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []
        self._values = FakeValuesEndpoint()

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

    def get(self, **kwargs: Any) -> FakeRequest:
        """Record spreadsheets get call."""
        return self._call('get', kwargs)

    def values(self) -> FakeValuesEndpoint:
        """Return values endpoint."""
        return self._values


class FakeSheetsService:
    """Expose fake Sheets endpoints."""

    def __init__(self) -> None:
        """Initialize Sheets service fake."""
        self._spreadsheets = FakeSpreadsheetsEndpoint()

    def spreadsheets(self) -> FakeSpreadsheetsEndpoint:
        """Return spreadsheets endpoint."""
        return self._spreadsheets


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
        uri='https://sheets.googleapis.com/v4/spreadsheets/secret123',
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


def test_batch_update_ranges_preserves_order_and_parameters(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        {
            'spreadsheetId': 'sheet-1',
            'totalUpdatedRows': 3,
            'totalUpdatedColumns': 3,
            'totalUpdatedCells': 4,
            'totalUpdatedSheets': 2,
            'responses': [
                {
                    'spreadsheetId': 'sheet-1',
                    'updatedRange': 'Sheet1!A1:B1',
                    'updatedRows': 1,
                    'updatedColumns': 2,
                    'updatedCells': 2,
                },
                {
                    'spreadsheetId': 'sheet-1',
                    'updatedRange': 'Sheet2!C1:C2',
                    'updatedRows': 2,
                    'updatedColumns': 1,
                    'updatedCells': 2,
                },
            ],
        },
    )
    data = (
        SheetsWriteRange(range_name='Sheet1!A1:B1', values=((1, 2),)),
        SheetsWriteRange(range_name='Sheet2!C1:C2', values=((3,), (4,))),
    )
    result = gateway.batch_update_ranges(
        'sheet-1',
        data,
        input_mode=SheetsInputMode.USER_ENTERED,
    )
    assert len(fake_service.spreadsheets().values().calls) == 1
    method, kwargs, req = fake_service.spreadsheets().values().calls[0]
    assert method == 'batchUpdate'
    assert kwargs == {
        'spreadsheetId': 'sheet-1',
        'body': {
            'valueInputOption': 'USER_ENTERED',
            'data': [
                {
                    'range': 'Sheet1!A1:B1',
                    'majorDimension': 'ROWS',
                    'values': [[1, 2]],
                },
                {
                    'range': 'Sheet2!C1:C2',
                    'majorDimension': 'ROWS',
                    'values': [[3], [4]],
                },
            ],
            'includeValuesInResponse': False,
        },
    }
    assert req.retries == [0]
    assert isinstance(result, SheetsBatchWriteResult)
    assert result.total_updated_rows == 3
    assert result.total_updated_columns == 3
    assert result.total_updated_cells == 4
    assert result.total_updated_sheets == 2
    assert len(result.responses) == 2
    assert result.responses[0] == SheetsWriteResult(
        spreadsheet_id='sheet-1',
        updated_range='Sheet1!A1:B1',
        updated_rows=1,
        updated_columns=2,
        updated_cells=2,
    )
    assert result.responses[1] == SheetsWriteResult(
        spreadsheet_id='sheet-1',
        updated_range='Sheet2!C1:C2',
        updated_rows=2,
        updated_columns=1,
        updated_cells=2,
    )


def test_batch_update_ranges_single_explicit_raw_mode(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        {
            'spreadsheetId': 'sheet-1',
            'totalUpdatedRows': 1,
            'totalUpdatedColumns': 1,
            'totalUpdatedCells': 1,
            'totalUpdatedSheets': 1,
            'responses': [
                {
                    'spreadsheetId': 'sheet-1',
                    'updatedRange': 'Sheet1!A1',
                    'updatedRows': 1,
                    'updatedColumns': 1,
                    'updatedCells': 1,
                }
            ],
        },
    )
    data = (SheetsWriteRange(range_name='Sheet1!A1', values=(('=1+2',),)),)
    gateway.batch_update_ranges(
        'sheet-1',
        data,
        input_mode=SheetsInputMode.RAW,
    )
    _, kwargs, _ = fake_service.spreadsheets().values().calls[0]
    assert kwargs['body']['valueInputOption'] == 'RAW'


def test_batch_update_ranges_range_limit_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway, store: FakeStore
) -> None:
    # 20 ranges is allowed
    data_20 = tuple(
        SheetsWriteRange(range_name=f'Sheet1!A{i}', values=((i,),))
        for i in range(1, 21)
    )
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        {
            'spreadsheetId': 'sheet-1',
            'totalUpdatedRows': 20,
            'totalUpdatedColumns': 1,
            'totalUpdatedCells': 20,
            'totalUpdatedSheets': 1,
            'responses': [
                {
                    'spreadsheetId': 'sheet-1',
                    'updatedRange': f'Sheet1!A{i}',
                    'updatedRows': 1,
                    'updatedColumns': 1,
                    'updatedCells': 1,
                }
                for i in range(1, 21)
            ],
        },
    )
    gateway.batch_update_ranges(
        'sheet-1',
        data_20,
        input_mode=SheetsInputMode.RAW,
    )
    assert store.calls == 1

    # 21 ranges rejected preauth
    data_21 = data_20 + (
        SheetsWriteRange(range_name='Sheet1!A21', values=((21,),)),
    )
    with pytest.raises(
        SheetsInputError,
        match=f'Cannot request more than {MAX_SHEETS_RANGES} ranges',
    ):
        gateway.batch_update_ranges(
            'sheet-1',
            data_21,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 1


def test_batch_update_ranges_total_cell_limit_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway, store: FakeStore
) -> None:
    # 10 ranges of 1000 cells each = 10,000 cells allowed
    row_100 = tuple(range(100))
    grid_1000 = tuple(row_100 for _ in range(10))  # 10x100 = 1000 cells
    data_10k = tuple(
        SheetsWriteRange(
            range_name=f'Sheet1!A{i * 10 + 1}:CV{i * 10 + 10}',
            values=grid_1000,
        )
        for i in range(10)
    )
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        {
            'spreadsheetId': 'sheet-1',
            'totalUpdatedCells': MAX_SHEETS_CELLS,
            'responses': [
                {
                    'spreadsheetId': 'sheet-1',
                    'updatedRange': f'Sheet1!A{i}',
                    'updatedRows': 10,
                    'updatedColumns': 100,
                    'updatedCells': 1000,
                }
                for i in range(10)
            ],
        },
    )
    gateway.batch_update_ranges(
        'sheet-1',
        data_10k,
        input_mode=SheetsInputMode.RAW,
    )
    assert store.calls == 1

    # Add one cell so 10001 cells rejected preauth
    data_10001 = data_10k + (
        SheetsWriteRange(range_name='Sheet2!A1', values=((1,),)),
    )
    with pytest.raises(SheetsInputError, match='cell limit exceeded'):
        gateway.batch_update_ranges(
            'sheet-1',
            data_10001,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 1


def test_batch_update_ranges_payload_limit_boundary(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    string_40k = 'X' * 40_000
    rows = tuple((string_40k,) for _ in range(30))
    data_large = tuple(
        SheetsWriteRange(range_name=f'Sheet1!A{i}', values=rows)
        for i in range(1, 2)
    )
    with pytest.raises(SheetsInputError, match='payload limit exceeded'):
        gateway.batch_update_ranges(
            'sheet-1',
            data_large,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_batch_update_ranges_rejects_empty_data(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(
        SheetsInputError, match='At least one range is required'
    ):
        gateway.batch_update_ranges(
            'sheet-1',
            (),
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_batch_update_ranges_rejects_str_or_bytes_data(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Data must be a sequence'):
        gateway.batch_update_ranges(
            'sheet-1',
            'invalid-str-data',  # type: ignore[arg-type]
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_batch_update_ranges_rejects_invalid_range_in_item(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    data = (
        SheetsWriteRange(range_name='Sheet1!A1', values=((1,),)),
        SheetsWriteRange(range_name='bad_range_name', values=((2,),)),
    )
    with pytest.raises(SheetsInputError):
        gateway.batch_update_ranges(
            'sheet-1',
            data,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_batch_update_ranges_rejects_invalid_scalar_in_item(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    data = (
        SheetsWriteRange(range_name='Sheet1!A1', values=(([1, 2],),)),  # type: ignore[arg-type]
    )
    with pytest.raises(SheetsInputError, match='Invalid cell value'):
        gateway.batch_update_ranges(
            'sheet-1',
            data,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_batch_update_ranges_maps_errors_safely(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        FakeRequest(error=_make_http_error(404)),
    )
    data = (SheetsWriteRange(range_name='Sheet1!A1', values=((1,),)),)
    with pytest.raises(SheetsNotFoundError):
        gateway.batch_update_ranges(
            'sheet-1',
            data,
            input_mode=SheetsInputMode.RAW,
        )


def test_clear_ranges_exact_kwargs_and_body(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchClear',
        {
            'spreadsheetId': 'sheet-1',
            'clearedRanges': ['Sheet1!A1:B2', 'Sheet2!C1:D5'],
        },
    )
    ranges = ('Sheet1!A1:B2', 'Sheet2!C1:D5')
    result = gateway.clear_ranges('sheet-1', ranges)

    assert len(fake_service.spreadsheets().values().calls) == 1
    method, kwargs, req = fake_service.spreadsheets().values().calls[0]
    assert method == 'batchClear'
    assert kwargs == {
        'spreadsheetId': 'sheet-1',
        'body': {'ranges': ['Sheet1!A1:B2', 'Sheet2!C1:D5']},
    }
    assert req.retries == [0]
    assert result == SheetsClearResult(
        spreadsheet_id='sheet-1',
        cleared_ranges=('Sheet1!A1:B2', 'Sheet2!C1:D5'),
    )


def test_clear_ranges_range_limit_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway, store: FakeStore
) -> None:
    ranges_20 = tuple(f'Sheet1!A{i}' for i in range(1, 21))
    fake_service.spreadsheets().values().queue(
        'batchClear',
        {
            'spreadsheetId': 'sheet-1',
            'clearedRanges': list(ranges_20),
        },
    )
    gateway.clear_ranges('sheet-1', ranges_20)
    assert store.calls == 1

    ranges_21 = ranges_20 + ('Sheet1!A21',)
    with pytest.raises(
        SheetsInputError,
        match=f'Cannot request more than {MAX_SHEETS_RANGES} ranges',
    ):
        gateway.clear_ranges('sheet-1', ranges_21)
    assert store.calls == 1


def test_clear_ranges_rejects_empty_ranges(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(
        SheetsInputError, match='At least one range is required'
    ):
        gateway.clear_ranges('sheet-1', ())
    assert store.calls == 0


def test_clear_ranges_rejects_str_or_bytes_ranges(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Ranges must be a sequence'):
        gateway.clear_ranges('sheet-1', 'Sheet1!A1')  # type: ignore[arg-type]
    assert store.calls == 0


def test_clear_ranges_rejects_empty_spreadsheet_id(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Spreadsheet ID is required'):
        gateway.clear_ranges('', ('Sheet1!A1',))
    assert store.calls == 0


def test_clear_ranges_rejects_invalid_a1_range(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError):
        gateway.clear_ranges('sheet-1', ('bad range!',))
    assert store.calls == 0


def test_clear_ranges_maps_errors_safely(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchClear',
        FakeRequest(error=_make_http_error(404)),
    )
    with pytest.raises(SheetsNotFoundError):
        gateway.clear_ranges('sheet-1', ('Sheet1!A1',))


def test_batch_update_ranges_rejects_missing_responses_field(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        {
            'spreadsheetId': 'sheet-1',
            'totalUpdatedRows': 1,
        },
    )
    data = (SheetsWriteRange(range_name='Sheet1!A1', values=((1,),)),)
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.batch_update_ranges(
            'sheet-1',
            data,
            input_mode=SheetsInputMode.RAW,
        )


def test_batch_update_ranges_rejects_mismatched_responses_count(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchUpdate',
        {
            'spreadsheetId': 'sheet-1',
            'responses': [
                {
                    'spreadsheetId': 'sheet-1',
                    'updatedRange': 'Sheet1!A1',
                    'updatedRows': 1,
                    'updatedColumns': 1,
                    'updatedCells': 1,
                }
            ],
        },
    )
    data = (
        SheetsWriteRange(range_name='Sheet1!A1', values=((1,),)),
        SheetsWriteRange(range_name='Sheet1!A2', values=((2,),)),
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.batch_update_ranges(
            'sheet-1',
            data,
            input_mode=SheetsInputMode.RAW,
        )


def test_clear_ranges_rejects_missing_cleared_ranges_field(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchClear',
        {
            'spreadsheetId': 'sheet-1',
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.clear_ranges('sheet-1', ('Sheet1!A1',))


def test_clear_ranges_rejects_mismatched_cleared_ranges_count(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'batchClear',
        {
            'spreadsheetId': 'sheet-1',
            'clearedRanges': ['Sheet1!A1'],
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.clear_ranges('sheet-1', ('Sheet1!A1', 'Sheet1!A2'))
