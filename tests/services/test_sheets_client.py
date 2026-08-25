"""Test Sheets provider gateway."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any
from unittest.mock import MagicMock, patch

import httplib2  # type: ignore[import-untyped]
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.services.sheets.client import (
    SheetsGateway,
    build_sheets_service,
)
from google_workspace_mcp.services.sheets.constants import (
    MAX_SHEETS_CELLS,
    MAX_SHEETS_RANGES,
    MAX_SHEETS_TEXT_CHARS,
    REQUEST_RETRIES,
)
from google_workspace_mcp.services.sheets.errors import (
    SheetsInputError,
    SheetsNotFoundError,
    SheetsProviderError,
    SheetsRateLimitError,
    SheetsScopeError,
)
from google_workspace_mcp.services.sheets.schemas import (
    MajorDimension,
    SheetsBatchReadResult,
    SheetsDateTimeMode,
    SheetsRenderMode,
    SheetSummary,
    SheetsValueRange,
    SpreadsheetSummary,
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


class FakeEndpoint:
    """Record Sheets endpoint calls."""

    def __init__(self) -> None:
        """Initialize Sheets endpoint fake."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        """Queue Sheets endpoint values."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record Sheets endpoint request."""
        if not self.responses[method]:
            raise AssertionError(f'No queued response for {method}({kwargs})')
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def get(self, **kwargs: Any) -> FakeRequest:
        """Record get call."""
        return self._call('get', kwargs)

    def batchGet(self, **kwargs: Any) -> FakeRequest:
        """Record batchGet call."""
        return self._call('batchGet', kwargs)


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


class FakeStore(GoogleCredentialStore):
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

    def refresh(self, request: Any = None) -> GoogleCredentials:
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


def test_build_sheets_service_creates_static_discovery_service() -> None:
    credentials = GoogleCredentials(
        token='test-token',
        refresh_token='test-refresh',
        client_id='test-client',
        client_secret='test-secret',
        scopes=('https://www.googleapis.com/auth/spreadsheets',),
    )
    with patch(
        'google_workspace_mcp.services.sheets.client.build'
    ) as mock_build:
        mock_build.return_value = 'mock-service'
        service = build_sheets_service(credentials)
        assert service == 'mock-service'
        assert mock_build.call_count == 1
        args, kwargs = mock_build.call_args
        assert args == ('sheets', 'v4')
        assert kwargs['cache_discovery'] is False
        assert kwargs['static_discovery'] is True
        assert kwargs['credentials'].token == 'test-token'


def test_gateway_service_builds_authenticated_service() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    assert gateway.service() is fake_service
    assert store.calls == 1


def test_gateway_service_maps_credential_errors_safely() -> None:

    def _failing_refresh() -> GoogleCredentials:
        """Raise simulated credential failure."""
        raise RuntimeError(
            'Sensitive token refresh failure at https://oauth2.googleapis.com'
        )

    store = MagicMock()
    store.refresh.side_effect = _failing_refresh
    gateway = SheetsGateway(store)
    with pytest.raises(
        SheetsProviderError, match='^Sheets credentials are unavailable$'
    ) as exc_info:
        gateway.service()
    assert 'oauth2' not in str(exc_info.value)
    assert 'token' not in str(exc_info.value)


def test_get_spreadsheet_retrieves_metadata() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().queue(
        'get',
        {
            'spreadsheetId': 'sheet-id-123',
            'properties': {
                'title': 'Quarterly Report',
                'locale': 'en_US',
                'timeZone': 'America/New_York',
            },
            'spreadsheetUrl': (
                'https://docs.google.com/spreadsheets/d/sheet-id-123/edit'
            ),
            'sheets': [
                {
                    'properties': {
                        'sheetId': 0,
                        'title': 'Q1',
                        'index': 0,
                        'sheetType': 'GRID',
                        'gridProperties': {
                            'rowCount': 100,
                            'columnCount': 26,
                        },
                    }
                },
                {
                    'properties': {
                        'sheetId': 101,
                        'title': 'ChartSheet',
                        'index': 1,
                        'sheetType': 'OBJECT',
                    }
                },
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    summary = gateway.get_spreadsheet('sheet-id-123')

    assert isinstance(summary, SpreadsheetSummary)
    assert summary.spreadsheet_id == 'sheet-id-123'
    assert summary.title == 'Quarterly Report'
    assert summary.locale == 'en_US'
    assert summary.time_zone == 'America/New_York'
    assert (
        summary.url
        == 'https://docs.google.com/spreadsheets/d/sheet-id-123/edit'
    )
    assert len(summary.sheets) == 2

    s0, s1 = summary.sheets
    assert s0 == SheetSummary(
        sheet_id=0,
        title='Q1',
        index=0,
        sheet_type='GRID',
        row_count=100,
        column_count=26,
    )
    assert s1 == SheetSummary(
        sheet_id=101,
        title='ChartSheet',
        index=1,
        sheet_type='OBJECT',
        row_count=None,
        column_count=None,
    )

    calls = fake_service.spreadsheets().calls
    assert len(calls) == 1
    method, kwargs, req = calls[0]
    assert method == 'get'
    assert kwargs == {
        'spreadsheetId': 'sheet-id-123',
        'includeGridData': False,
    }
    assert req.retries == [REQUEST_RETRIES]


def test_get_spreadsheet_validates_input() -> None:
    store = FakeStore()
    gateway = SheetsGateway(store)
    with pytest.raises(SheetsInputError, match='Spreadsheet ID'):
        gateway.get_spreadsheet('')
    with pytest.raises(SheetsInputError, match='Spreadsheet ID'):
        gateway.get_spreadsheet('   ')
    assert store.calls == 0


def test_read_range_formatted() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:B2',
            'majorDimension': 'ROWS',
            'values': [
                ['Item', 'Cost'],
                ['Widget', '$10.00'],
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    result = gateway.read_range(
        'sheet-123',
        'Sheet1!A1:B2',
        render_mode=SheetsRenderMode.FORMATTED,
    )

    assert isinstance(result, SheetsValueRange)
    assert result.requested_range == 'Sheet1!A1:B2'
    assert result.resolved_range == 'Sheet1!A1:B2'
    assert result.major_dimension == MajorDimension.ROWS
    assert result.values == (
        ('Item', 'Cost'),
        ('Widget', '$10.00'),
    )
    assert result.row_count == 2
    assert result.column_count == 2
    assert result.cell_count == 4

    calls = fake_service.spreadsheets().values().calls
    assert len(calls) == 1
    method, kwargs, req = calls[0]
    assert method == 'get'
    assert kwargs == {
        'spreadsheetId': 'sheet-123',
        'range': 'Sheet1!A1:B2',
        'valueRenderOption': 'FORMATTED_VALUE',
        'majorDimension': 'ROWS',
    }
    assert req.retries == [REQUEST_RETRIES]


def test_read_range_unformatted_and_datetime_mode() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:A2',
            'majorDimension': 'ROWS',
            'values': [
                [44927.5],
                [44928.0],
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    result = gateway.read_range(
        'sheet-123',
        'Sheet1!A1:A2',
        render_mode=SheetsRenderMode.UNFORMATTED,
        date_time_mode=SheetsDateTimeMode.SERIAL_NUMBER,
        major_dimension=MajorDimension.ROWS,
    )

    assert result.values == ((44927.5,), (44928.0,))
    assert result.row_count == 2
    assert result.column_count == 1
    assert result.cell_count == 2

    calls = fake_service.spreadsheets().values().calls
    assert len(calls) == 1
    _, kwargs, _ = calls[0]
    assert kwargs == {
        'spreadsheetId': 'sheet-123',
        'range': 'Sheet1!A1:A2',
        'valueRenderOption': 'UNFORMATTED_VALUE',
        'dateTimeRenderOption': 'SERIAL_NUMBER',
        'majorDimension': 'ROWS',
    }


def test_read_range_columns_dimension() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:C2',
            'majorDimension': 'COLUMNS',
            'values': [
                ['A1', 'A2'],
                ['B1', 'B2'],
                ['C1', 'C2'],
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    result = gateway.read_range(
        'sheet-123',
        'Sheet1!A1:C2',
        render_mode=SheetsRenderMode.FORMATTED,
        major_dimension=MajorDimension.COLUMNS,
    )

    assert result.major_dimension == MajorDimension.COLUMNS
    assert result.values == (
        ('A1', 'A2'),
        ('B1', 'B2'),
        ('C1', 'C2'),
    )
    assert result.row_count == 2
    assert result.column_count == 3
    assert result.cell_count == 6


def test_read_range_trailing_empties_no_inference() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:D4',
            'majorDimension': 'ROWS',
            'values': [
                ['Header1', 'Header2'],
                ['Val1'],
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    result = gateway.read_range(
        'sheet-123',
        'Sheet1!A1:D4',
        render_mode=SheetsRenderMode.FORMATTED,
    )

    assert result.requested_range == 'Sheet1!A1:D4'
    assert result.resolved_range == 'Sheet1!A1:D4'
    assert result.values == (
        ('Header1', 'Header2'),
        ('Val1',),
    )
    assert result.row_count == 2
    assert result.column_count == 2
    assert result.cell_count == 3


def test_read_range_empty_values_returns_empty_tuple() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:B2',
            'majorDimension': 'ROWS',
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    result = gateway.read_range(
        'sheet-123',
        'Sheet1!A1:B2',
        render_mode=SheetsRenderMode.FORMATTED,
    )

    assert result.values == ()
    assert result.row_count == 0
    assert result.column_count == 0
    assert result.cell_count == 0


def test_read_range_enforces_exact_mode_pairing() -> None:
    store = FakeStore()
    gateway = SheetsGateway(store)

    # FORMATTED forbids date_time_mode
    with pytest.raises(SheetsInputError, match='date_time_mode'):
        gateway.read_range(
            'sheet-123',
            'Sheet1!A1:B2',
            render_mode=SheetsRenderMode.FORMATTED,
            date_time_mode=SheetsDateTimeMode.SERIAL_NUMBER,
        )

    # UNFORMATTED requires date_time_mode
    with pytest.raises(SheetsInputError, match='date_time_mode is required'):
        gateway.read_range(
            'sheet-123',
            'Sheet1!A1:B2',
            render_mode=SheetsRenderMode.UNFORMATTED,
            date_time_mode=None,
        )

    # FORMULA requires date_time_mode
    with pytest.raises(SheetsInputError, match='date_time_mode is required'):
        gateway.read_range(
            'sheet-123',
            'Sheet1!A1:B2',
            render_mode=SheetsRenderMode.FORMULA,
            date_time_mode=None,
        )

    assert store.calls == 0


def test_read_range_validates_a1_before_refresh() -> None:
    store = FakeStore()
    gateway = SheetsGateway(store)
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        gateway.read_range(
            'sheet-123', 'A1:B2', render_mode=SheetsRenderMode.FORMATTED
        )
    with pytest.raises(SheetsInputError, match='invalid'):
        gateway.read_range(
            'sheet-123', 'Sheet1!A:B', render_mode=SheetsRenderMode.FORMATTED
        )
    assert store.calls == 0


def test_batch_read_ranges_preserves_order_and_parameters() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'batchGet',
        {
            'spreadsheetId': 'sheet-123',
            'valueRanges': [
                {
                    'range': 'Sheet1!A1:A2',
                    'majorDimension': 'ROWS',
                    'values': [['R1C1'], ['R2C1']],
                },
                {
                    'range': 'Sheet2!B1:C1',
                    'majorDimension': 'ROWS',
                    'values': [['S2B1', 'S2C1']],
                },
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    result = gateway.batch_read_ranges(
        'sheet-123',
        ('Sheet1!A1:A2', 'Sheet2!B1:C1'),
        render_mode=SheetsRenderMode.FORMULA,
        date_time_mode=SheetsDateTimeMode.FORMATTED_STRING,
        major_dimension=MajorDimension.ROWS,
    )

    assert isinstance(result, SheetsBatchReadResult)
    assert result.spreadsheet_id == 'sheet-123'
    assert len(result.ranges) == 2
    r0, r1 = result.ranges

    assert r0.requested_range == 'Sheet1!A1:A2'
    assert r0.resolved_range == 'Sheet1!A1:A2'
    assert r0.values == (('R1C1',), ('R2C1',))
    assert r0.cell_count == 2

    assert r1.requested_range == 'Sheet2!B1:C1'
    assert r1.resolved_range == 'Sheet2!B1:C1'
    assert r1.values == (('S2B1', 'S2C1'),)
    assert r1.cell_count == 2

    calls = fake_service.spreadsheets().values().calls
    assert len(calls) == 1
    method, kwargs, req = calls[0]
    assert method == 'batchGet'
    assert kwargs == {
        'spreadsheetId': 'sheet-123',
        'ranges': ['Sheet1!A1:A2', 'Sheet2!B1:C1'],
        'valueRenderOption': 'FORMULA',
        'dateTimeRenderOption': 'FORMATTED_STRING',
        'majorDimension': 'ROWS',
    }
    assert req.retries == [REQUEST_RETRIES]


def test_batch_read_ranges_validates_bounds_before_refresh() -> None:
    store = FakeStore()
    gateway = SheetsGateway(store)

    # Reject str or bytes passed as ranges
    with pytest.raises(SheetsInputError, match='sequence of range strings'):
        gateway.batch_read_ranges(
            'sheet-123',
            'Sheet1!A1:B2',  # type: ignore[arg-type]
            render_mode=SheetsRenderMode.FORMATTED,
        )
    with pytest.raises(SheetsInputError, match='sequence of range strings'):
        gateway.batch_read_ranges(
            'sheet-123',
            b'Sheet1!A1:B2',  # type: ignore[arg-type]
            render_mode=SheetsRenderMode.FORMATTED,
        )

    # Empty ranges
    with pytest.raises(SheetsInputError, match='At least one range'):
        gateway.batch_read_ranges(
            'sheet-123', (), render_mode=SheetsRenderMode.FORMATTED
        )

    # Exceeding MAX_SHEETS_RANGES
    excess_ranges = tuple(
        f'Sheet1!A{i}:B{i}' for i in range(MAX_SHEETS_RANGES + 1)
    )
    with pytest.raises(
        SheetsInputError, match=f'more than {MAX_SHEETS_RANGES}'
    ):
        gateway.batch_read_ranges(
            'sheet-123', excess_ranges, render_mode=SheetsRenderMode.FORMATTED
        )

    # Invalid range in batch
    with pytest.raises(SheetsInputError, match='sheet qualified'):
        gateway.batch_read_ranges(
            'sheet-123',
            ('Sheet1!A1:A2', 'A1:B2'),
            render_mode=SheetsRenderMode.FORMATTED,
        )

    # date_time_mode with formatted
    with pytest.raises(SheetsInputError, match='date_time_mode'):
        gateway.batch_read_ranges(
            'sheet-123',
            ('Sheet1!A1:A2',),
            render_mode=SheetsRenderMode.FORMATTED,
            date_time_mode=SheetsDateTimeMode.FORMATTED_STRING,
        )

    # UNFORMATTED requires date_time_mode
    with pytest.raises(SheetsInputError, match='date_time_mode is required'):
        gateway.batch_read_ranges(
            'sheet-123',
            ('Sheet1!A1:A2',),
            render_mode=SheetsRenderMode.UNFORMATTED,
            date_time_mode=None,
        )

    assert store.calls == 0


def test_batch_read_ranges_enforces_cell_limit() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    huge_row = list(range(MAX_SHEETS_CELLS + 1))
    fake_service.spreadsheets().values().queue(
        'batchGet',
        {
            'spreadsheetId': 'sheet-123',
            'valueRanges': [
                {
                    'range': 'Sheet1!A1:Z100',
                    'majorDimension': 'ROWS',
                    'values': [huge_row],
                }
            ],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    with pytest.raises(
        SheetsProviderError, match='Response exceeded maximum cell count'
    ):
        gateway.batch_read_ranges(
            'sheet-123',
            ('Sheet1!A1:Z100',),
            render_mode=SheetsRenderMode.FORMATTED,
        )


def test_batch_read_ranges_mismatched_value_ranges_raises() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().values().queue(
        'batchGet',
        {
            'spreadsheetId': 'sheet-123',
            'valueRanges': [],
        },
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.batch_read_ranges(
            'sheet-123',
            ('Sheet1!A1:A2',),
            render_mode=SheetsRenderMode.FORMATTED,
        )


def test_cell_scalar_validation() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)

    # Valid mixed scalar cells (None, bool, int, float, str)
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:E1',
            'majorDimension': 'ROWS',
            'values': [[None, True, False, 42, 3.14, 'hello']],
        },
    )
    result = gateway.read_range(
        'sheet-123',
        'Sheet1!A1:E1',
        render_mode=SheetsRenderMode.FORMATTED,
    )
    assert result.values == ((None, True, False, 42, 3.14, 'hello'),)

    # Reject mapping cell
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:A1',
            'majorDimension': 'ROWS',
            'values': [[{'key': 'value'}]],
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.read_range(
            'sheet-123', 'Sheet1!A1:A1', render_mode=SheetsRenderMode.FORMATTED
        )

    # Reject nested list cell
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:A1',
            'majorDimension': 'ROWS',
            'values': [[['nested', 'list']]],
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.read_range(
            'sheet-123', 'Sheet1!A1:A1', render_mode=SheetsRenderMode.FORMATTED
        )

    # Reject NaN or Inf
    for non_finite in (float('nan'), float('inf'), float('-inf')):
        fake_service.spreadsheets().values().queue(
            'get',
            {
                'range': 'Sheet1!A1:A1',
                'majorDimension': 'ROWS',
                'values': [[non_finite]],
            },
        )
        with pytest.raises(
            SheetsProviderError, match='Sheets returned an invalid response'
        ):
            gateway.read_range(
                'sheet-123',
                'Sheet1!A1:A1',
                render_mode=SheetsRenderMode.FORMATTED,
            )

    # Reject oversized cell string
    oversized = 'X' * (MAX_SHEETS_TEXT_CHARS + 1)
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:A1',
            'majorDimension': 'ROWS',
            'values': [[oversized]],
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.read_range(
            'sheet-123', 'Sheet1!A1:A1', render_mode=SheetsRenderMode.FORMATTED
        )

    # Reject null byte in cell string
    fake_service.spreadsheets().values().queue(
        'get',
        {
            'range': 'Sheet1!A1:A1',
            'majorDimension': 'ROWS',
            'values': [['bad\x00string']],
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.read_range(
            'sheet-123', 'Sheet1!A1:A1', render_mode=SheetsRenderMode.FORMATTED
        )


def test_error_mapping_404_not_found() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(404, 'notFound')),
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    with pytest.raises(
        SheetsNotFoundError, match='^Sheets resource was not found$'
    ) as exc_info:
        gateway.get_spreadsheet('sheet-123')
    assert 'secret123' not in str(exc_info.value)
    assert 'https://' not in str(exc_info.value)


def test_error_mapping_403_insufficient_scope() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(403, 'insufficientPermissions')),
    )
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)
    with pytest.raises(
        SheetsScopeError,
        match='^Google authorization lacks required Sheets permissions$',
    ) as exc_info:
        gateway.get_spreadsheet('sheet-123')
    assert 'secret123' not in str(exc_info.value)

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(
            error=_make_http_error(403, 'ACCESS_TOKEN_SCOPE_INSUFFICIENT')
        ),
    )
    with pytest.raises(SheetsScopeError):
        gateway.get_spreadsheet('sheet-123')


def test_error_mapping_rate_limit_reasons() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(429)),
    )
    with pytest.raises(
        SheetsRateLimitError, match='^Sheets is temporarily rate limited$'
    ):
        gateway.get_spreadsheet('sheet-123')

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(403, 'userRateLimitExceeded')),
    )
    with pytest.raises(
        SheetsRateLimitError, match='^Sheets is temporarily rate limited$'
    ):
        gateway.get_spreadsheet('sheet-123')

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(403, 'rateLimitExceeded')),
    )
    with pytest.raises(
        SheetsRateLimitError, match='^Sheets is temporarily rate limited$'
    ):
        gateway.get_spreadsheet('sheet-123')


def test_error_mapping_other_http_statuses() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(401)),
    )
    with pytest.raises(
        SheetsProviderError, match='^Google authorization requires renewal$'
    ):
        gateway.get_spreadsheet('sheet-123')

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(400)),
    )
    with pytest.raises(
        SheetsProviderError, match='^Sheets rejected the request$'
    ):
        gateway.get_spreadsheet('sheet-123')

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(403, 'unknownReason')),
    )
    with pytest.raises(
        SheetsProviderError, match='^Sheets request was forbidden$'
    ):
        gateway.get_spreadsheet('sheet-123')

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=_make_http_error(503)),
    )
    with pytest.raises(
        SheetsProviderError,
        match='^Sheets request is temporarily unavailable$',
    ):
        gateway.get_spreadsheet('sheet-123')


def test_error_mapping_transport_and_os_errors() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=TransportError('Connection timed out to 10.0.0.1')),
    )
    with pytest.raises(
        SheetsProviderError,
        match='^Sheets request is temporarily unavailable$',
    ) as exc_info:
        gateway.get_spreadsheet('sheet-123')
    assert '10.0.0.1' not in str(exc_info.value)

    fake_service.spreadsheets().queue(
        'get',
        FakeRequest(error=ConnectionResetError('Reset by peer')),
    )
    with pytest.raises(
        SheetsProviderError,
        match='^Sheets request is temporarily unavailable$',
    ):
        gateway.get_spreadsheet('sheet-123')


def test_response_validation_rejects_invalid_structures() -> None:
    store = FakeStore()
    fake_service = FakeSheetsService()
    gateway = SheetsGateway(store, service_builder=lambda _: fake_service)

    fake_service.spreadsheets().queue(
        'get',
        'not a dict',
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.get_spreadsheet('sheet-123')

    fake_service.spreadsheets().queue(
        'get',
        {
            'spreadsheetId': 'sheet-123',
            'properties': {'title': 'Test'},
            'sheets': 12345,
        },
    )
    with pytest.raises(
        SheetsProviderError, match='Sheets returned an invalid response'
    ):
        gateway.get_spreadsheet('sheet-123')
