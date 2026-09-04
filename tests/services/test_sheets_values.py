"""Test Sheets value operations."""

import json
from collections import defaultdict, deque
from typing import Any

import httplib2  # type: ignore[import-untyped]
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import GoogleCredentials
from google_workspace_mcp.services.sheets.client import SheetsGateway
from google_workspace_mcp.services.sheets.constants import MAX_SHEETS_CELLS
from google_workspace_mcp.services.sheets.errors import (
    SheetsInputError,
    SheetsNotFoundError,
    SheetsProviderError,
    SheetsRateLimitError,
    SheetsScopeError,
)
from google_workspace_mcp.services.sheets.schemas import (
    SheetsAppendResult,
    SheetsInputMode,
    SheetsInsertMode,
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

    @property
    def last_value_input_option(self) -> str | None:
        """Return last input option."""
        if not self._spreadsheets._values.calls:
            return None
        _, kwargs, _ = self._spreadsheets._values.calls[-1]
        if 'valueInputOption' in kwargs:
            return kwargs['valueInputOption']
        body = kwargs.get('body')
        if isinstance(body, dict) and 'valueInputOption' in body:
            return body['valueInputOption']
        return None

    @property
    def last_insert_data_option(self) -> str | None:
        """Return last insert option."""
        if not self._spreadsheets._values.calls:
            return None
        _, kwargs, _ = self._spreadsheets._values.calls[-1]
        return kwargs.get('insertDataOption')


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


def test_raw_formula_stays_raw(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRange': "'Data'!A1",
            'updatedRows': 1,
            'updatedColumns': 1,
            'updatedCells': 1,
        },
    )
    result = gateway.update_range(
        'sheet-1',
        "'Data'!A1",
        (('=1+2',),),
        input_mode=SheetsInputMode.RAW,
    )
    assert fake_service.last_value_input_option == 'RAW'
    assert isinstance(result, SheetsWriteResult)
    assert result.updated_range == "'Data'!A1"


def test_user_entered_formula_is_explicit(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRange': "'Data'!A1",
            'updatedRows': 1,
            'updatedColumns': 1,
            'updatedCells': 1,
        },
    )
    result = gateway.update_range(
        'sheet-1',
        "'Data'!A1",
        (('=1+2',),),
        input_mode=SheetsInputMode.USER_ENTERED,
    )
    assert fake_service.last_value_input_option == 'USER_ENTERED'
    assert isinstance(result, SheetsWriteResult)


def test_update_range_exact_kwargs_and_body(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-abc',
            'updatedRange': 'Sheet1!A1:B2',
            'updatedRows': 2,
            'updatedColumns': 2,
            'updatedCells': 4,
        },
    )
    values = ((1, 'hello'), (True, None))
    result = gateway.update_range(
        'sheet-abc',
        'Sheet1!A1:B2',
        values,
        input_mode=SheetsInputMode.RAW,
    )
    assert len(fake_service.spreadsheets().values().calls) == 1
    method, kwargs, req = fake_service.spreadsheets().values().calls[0]
    assert method == 'update'
    assert kwargs == {
        'spreadsheetId': 'sheet-abc',
        'range': 'Sheet1!A1:B2',
        'valueInputOption': 'RAW',
        'includeValuesInResponse': False,
        'body': {'values': [[1, 'hello'], [True, None]]},
    }
    assert req.retries == [0]
    assert result == SheetsWriteResult(
        spreadsheet_id='sheet-abc',
        updated_range='Sheet1!A1:B2',
        updated_rows=2,
        updated_columns=2,
        updated_cells=4,
    )


def test_update_range_missing_updated_counts_defaults_zero(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-abc',
            'updatedRange': 'Sheet1!A1',
        },
    )
    result = gateway.update_range(
        'sheet-abc',
        'Sheet1!A1',
        (('text',),),
        input_mode=SheetsInputMode.USER_ENTERED,
    )
    assert result.updated_rows == 0
    assert result.updated_columns == 0
    assert result.updated_cells == 0


def test_update_range_rejects_empty_spreadsheet_id(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Spreadsheet ID is required'):
        gateway.update_range(
            '   ',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_update_range_rejects_invalid_a1_range(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError):
        gateway.update_range(
            'sheet-1',
            'Invalid Range Name',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_update_range_rejects_non_sequence_values(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Values must be a sequence'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            'raw-string',  # type: ignore[arg-type]
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_update_range_rejects_string_as_row(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Row must be a sequence'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1:A2',
            ('row1_string', 'row2_string'),  # type: ignore[arg-type]
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_update_range_rejects_invalid_scalar_cells(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Invalid cell value'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            (({'key': 'value'},),),  # type: ignore[arg-type]
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_update_range_accepts_valid_scalars(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRange': 'Sheet1!A1:E1',
            'updatedRows': 1,
            'updatedColumns': 5,
            'updatedCells': 5,
        },
    )
    result = gateway.update_range(
        'sheet-1',
        'Sheet1!A1:E1',
        ((None, True, 42, 3.14, 'hello'),),
        input_mode=SheetsInputMode.RAW,
    )
    assert result.updated_cells == 5


def test_update_range_cell_limit_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway, store: FakeStore
) -> None:
    row_100 = tuple(range(100))
    grid_10k = tuple(row_100 for _ in range(100))
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRange': 'Sheet1!A1:CV100',
            'updatedRows': 100,
            'updatedColumns': 100,
            'updatedCells': MAX_SHEETS_CELLS,
        },
    )
    gateway.update_range(
        'sheet-1',
        'Sheet1!A1:CV100',
        grid_10k,
        input_mode=SheetsInputMode.RAW,
    )
    assert store.calls == 1

    grid_10001 = grid_10k + ((1,),)
    with pytest.raises(SheetsInputError, match='cell limit exceeded'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1:CV101',
            grid_10001,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 1


def test_update_range_payload_limit_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway, store: FakeStore
) -> None:
    large_string = 'A' * 12_000
    rows_large = tuple((large_string,) for _ in range(100))
    with pytest.raises(SheetsInputError, match='payload limit exceeded'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1:A100',
            rows_large,
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_update_range_per_cell_text_length_boundary(
    fake_service: FakeSheetsService, gateway: SheetsGateway, store: FakeStore
) -> None:
    text_50k = 'A' * 50_000
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRange': 'Sheet1!A1',
            'updatedRows': 1,
            'updatedColumns': 1,
            'updatedCells': 1,
        },
    )
    gateway.update_range(
        'sheet-1',
        'Sheet1!A1',
        ((text_50k,),),
        input_mode=SheetsInputMode.RAW,
    )
    assert store.calls == 1

    text_50001 = 'A' * 50_001
    with pytest.raises(SheetsInputError, match='Invalid cell value'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((text_50001,),),
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 1


def test_update_range_rejects_null_byte_in_string_cell(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Invalid cell value'):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            (('hello\x00world',),),
            input_mode=SheetsInputMode.RAW,
        )
    assert store.calls == 0


def test_append_rows_insert_mode_insert_rows(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append',
        {
            'spreadsheetId': 'sheet-1',
            'tableRange': 'Sheet1!A1:B10',
            'updates': {
                'updatedRange': 'Sheet1!A11:B11',
                'updatedRows': 1,
                'updatedColumns': 2,
                'updatedCells': 2,
            },
        },
    )
    result = gateway.append_rows(
        'sheet-1',
        'Sheet1!A1:B1',
        (('val1', 'val2'),),
        input_mode=SheetsInputMode.RAW,
        insert_mode=SheetsInsertMode.INSERT_ROWS,
    )
    assert fake_service.last_insert_data_option == 'INSERT_ROWS'
    assert fake_service.last_value_input_option == 'RAW'
    assert isinstance(result, SheetsAppendResult)
    assert result.table_range == 'Sheet1!A1:B10'
    assert result.updated_range == 'Sheet1!A11:B11'
    assert result.updated_rows == 1
    assert result.updated_columns == 2
    assert result.updated_cells == 2


def test_append_rows_insert_mode_overwrite(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append',
        {
            'spreadsheetId': 'sheet-1',
            'updates': {
                'updatedRange': 'Sheet1!A11:B11',
                'updatedRows': 1,
                'updatedColumns': 2,
                'updatedCells': 2,
            },
        },
    )
    result = gateway.append_rows(
        'sheet-1',
        'Sheet1!A1:B1',
        (('val1', 'val2'),),
        input_mode=SheetsInputMode.USER_ENTERED,
        insert_mode=SheetsInsertMode.OVERWRITE,
    )
    assert fake_service.last_insert_data_option == 'OVERWRITE'
    assert fake_service.last_value_input_option == 'USER_ENTERED'
    assert result.table_range is None


def test_append_rows_exact_kwargs_and_body(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append',
        {
            'spreadsheetId': 'sheet-1',
            'tableRange': 'Sheet1!A1:C5',
            'updates': {
                'updatedRange': 'Sheet1!A6:C6',
                'updatedRows': 1,
                'updatedColumns': 3,
                'updatedCells': 3,
            },
        },
    )
    values = ((1, 2, 3),)
    gateway.append_rows(
        'sheet-1',
        'Sheet1!A1:C1',
        values,
        input_mode=SheetsInputMode.RAW,
        insert_mode=SheetsInsertMode.INSERT_ROWS,
    )
    assert len(fake_service.spreadsheets().values().calls) == 1
    method, kwargs, req = fake_service.spreadsheets().values().calls[0]
    assert method == 'append'
    assert kwargs == {
        'spreadsheetId': 'sheet-1',
        'range': 'Sheet1!A1:C1',
        'valueInputOption': 'RAW',
        'insertDataOption': 'INSERT_ROWS',
        'includeValuesInResponse': False,
        'body': {'values': [[1, 2, 3]]},
    }
    assert req.retries == [0]


def test_append_rows_table_range_empty_string_maps_to_none(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append',
        {
            'spreadsheetId': 'sheet-1',
            'tableRange': '',
            'updates': {
                'updatedRange': 'Sheet1!A1:B1',
                'updatedRows': 1,
                'updatedColumns': 2,
                'updatedCells': 2,
            },
        },
    )
    result = gateway.append_rows(
        'sheet-1',
        'Sheet1!A1:B1',
        (('v1', 'v2'),),
        input_mode=SheetsInputMode.RAW,
        insert_mode=SheetsInsertMode.INSERT_ROWS,
    )
    assert result.table_range is None


def test_append_rows_validates_bounds_before_refresh(
    gateway: SheetsGateway, store: FakeStore
) -> None:
    with pytest.raises(SheetsInputError, match='Spreadsheet ID is required'):
        gateway.append_rows(
            '',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
            insert_mode=SheetsInsertMode.INSERT_ROWS,
        )
    assert store.calls == 0

    with pytest.raises(SheetsInputError):
        gateway.append_rows(
            'sheet-1',
            'bad_range!',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
            insert_mode=SheetsInsertMode.INSERT_ROWS,
        )
    assert store.calls == 0

    with pytest.raises(SheetsInputError, match='Values must be a sequence'):
        gateway.append_rows(
            'sheet-1',
            'Sheet1!A1',
            123,  # type: ignore[arg-type]
            input_mode=SheetsInputMode.RAW,
            insert_mode=SheetsInsertMode.INSERT_ROWS,
        )
    assert store.calls == 0


def test_update_range_maps_errors_safely(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        FakeRequest(error=_make_http_error(404)),
    )
    with pytest.raises(
        SheetsNotFoundError, match='Sheets resource was not found'
    ):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )

    fake_service.spreadsheets().values().queue(
        'update',
        FakeRequest(error=_make_http_error(403, 'insufficientPermissions')),
    )
    with pytest.raises(SheetsScopeError):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )

    fake_service.spreadsheets().values().queue(
        'update',
        FakeRequest(error=_make_http_error(429, 'rateLimitExceeded')),
    )
    with pytest.raises(SheetsRateLimitError):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )

    fake_service.spreadsheets().values().queue(
        'update',
        FakeRequest(error=TransportError('Connection reset')),
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets request is temporarily unavailable'
    ):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )


def test_append_rows_maps_errors_safely(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append',
        FakeRequest(error=_make_http_error(404)),
    )
    with pytest.raises(SheetsNotFoundError):
        gateway.append_rows(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
            insert_mode=SheetsInsertMode.INSERT_ROWS,
        )


def test_update_range_invalid_provider_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue('update', 'not-a-dict')
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )


def test_append_rows_invalid_provider_response(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append', {'spreadsheetId': 's1'}
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.append_rows(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
            insert_mode=SheetsInsertMode.INSERT_ROWS,
        )


def test_update_range_rejects_missing_or_empty_updated_range(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRows': 1,
            'updatedColumns': 1,
            'updatedCells': 1,
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )

    fake_service.spreadsheets().values().queue(
        'update',
        {
            'spreadsheetId': 'sheet-1',
            'updatedRange': '   ',
            'updatedRows': 1,
            'updatedColumns': 1,
            'updatedCells': 1,
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.update_range(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
        )


def test_append_rows_rejects_missing_or_empty_updated_range(
    fake_service: FakeSheetsService, gateway: SheetsGateway
) -> None:
    fake_service.spreadsheets().values().queue(
        'append',
        {
            'spreadsheetId': 'sheet-1',
            'updates': {
                'updatedRows': 1,
                'updatedColumns': 1,
                'updatedCells': 1,
            },
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.append_rows(
            'sheet-1',
            'Sheet1!A1',
            ((1,),),
            input_mode=SheetsInputMode.RAW,
            insert_mode=SheetsInsertMode.INSERT_ROWS,
        )
