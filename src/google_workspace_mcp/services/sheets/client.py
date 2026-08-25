"""Call Sheets provider methods."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)

from .a1 import validate_a1_range
from .constants import (
    MAX_SHEETS_CELLS,
    MAX_SHEETS_GRID_CELLS,
    MAX_SHEETS_PAYLOAD_BYTES,
    MAX_SHEETS_RANGES,
    MAX_SHEETS_TEXT_CHARS,
    MAX_SHEETS_TITLE_CHARS,
    REQUEST_RETRIES,
)
from .errors import (
    SheetsError,
    SheetsInputError,
    SheetsNotFoundError,
    SheetsProviderError,
    SheetsRateLimitError,
    SheetsScopeError,
)
from .schemas import (
    MajorDimension,
    SheetCopyResult,
    SheetMutationResult,
    SheetsAppendResult,
    SheetsBatchReadResult,
    SheetsBatchWriteResult,
    SheetsClearResult,
    SheetsDateTimeMode,
    SheetsInputMode,
    SheetsInsertMode,
    SheetsRenderMode,
    SheetSummary,
    SheetsValueRange,
    SheetsWriteRange,
    SheetsWriteResult,
    SpreadsheetCreateResult,
    SpreadsheetSummary,
)

ServiceBuilder = Callable[[GoogleCredentials], Any]

_SAFE_REASONS = frozenset(
    {
        'rateLimitExceeded',
        'userRateLimitExceeded',
        'quotaExceeded',
        'insufficientPermissions',
        'insufficientFilePermissions',
        'insufficientScope',
        'ACCESS_TOKEN_SCOPE_INSUFFICIENT',
    }
)

_SCOPE_REASONS = frozenset(
    {
        'insufficientPermissions',
        'insufficientFilePermissions',
        'insufficientScope',
        'ACCESS_TOKEN_SCOPE_INSUFFICIENT',
    }
)

_RATE_LIMIT_REASONS = frozenset(
    {
        'rateLimitExceeded',
        'userRateLimitExceeded',
        'quotaExceeded',
    }
)

_VALUE_RENDER_OPTIONS: dict[SheetsRenderMode, str] = {
    SheetsRenderMode.FORMATTED: 'FORMATTED_VALUE',
    SheetsRenderMode.UNFORMATTED: 'UNFORMATTED_VALUE',
    SheetsRenderMode.FORMULA: 'FORMULA',
}

_MAJOR_DIMENSIONS: dict[MajorDimension, str] = {
    MajorDimension.ROWS: 'ROWS',
    MajorDimension.COLUMNS: 'COLUMNS',
}

_DATE_TIME_RENDER_OPTIONS: dict[SheetsDateTimeMode, str] = {
    SheetsDateTimeMode.SERIAL_NUMBER: 'SERIAL_NUMBER',
    SheetsDateTimeMode.FORMATTED_STRING: 'FORMATTED_STRING',
}
_VALUE_INPUT_OPTIONS: dict[SheetsInputMode, str] = {
    SheetsInputMode.RAW: 'RAW',
    SheetsInputMode.USER_ENTERED: 'USER_ENTERED',
}

_INSERT_DATA_OPTIONS: dict[SheetsInsertMode, str] = {
    SheetsInsertMode.INSERT_ROWS: 'INSERT_ROWS',
    SheetsInsertMode.OVERWRITE: 'OVERWRITE',
}


def build_sheets_service(credentials: GoogleCredentials) -> Any:
    """Build Sheets provider service."""
    return build(
        'sheets',
        'v4',
        credentials=credentials.to_google_credentials(),
        cache_discovery=False,
        static_discovery=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    """Require Sheets response mapping."""
    if not isinstance(value, Mapping):
        raise SheetsProviderError('Sheets returned an invalid response')
    return value


def _sequence(value: Any, limit: int) -> Sequence[Any]:
    """Require bounded Sheets collection."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise SheetsProviderError('Sheets returned an invalid response')
    if len(value) > limit:
        raise SheetsProviderError('Sheets returned an invalid response')
    return value


def _text(value: Any, limit: int = MAX_SHEETS_TEXT_CHARS) -> str:
    """Validate bounded Sheets text."""
    if not isinstance(value, str) or len(value) > limit:
        raise SheetsProviderError('Sheets returned an invalid response')
    return value


def _optional_text(
    value: Any, limit: int = MAX_SHEETS_TEXT_CHARS
) -> str | None:
    """Validate optional bounded text."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise SheetsProviderError('Sheets returned an invalid response')
    return value


def _integer(value: Any, min_val: int = 0, max_val: int | None = None) -> int:
    """Validate bounded Sheets integer."""
    if isinstance(value, bool):
        raise SheetsProviderError('Sheets returned an invalid response')
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            raise SheetsProviderError(
                'Sheets returned an invalid response'
            ) from None
    else:
        raise SheetsProviderError('Sheets returned an invalid response')

    if parsed < min_val or (max_val is not None and parsed > max_val):
        raise SheetsProviderError('Sheets returned an invalid response')
    return parsed


def _optional_integer(
    value: Any, min_val: int = 0, max_val: int | None = None
) -> int | None:
    """Validate optional Sheets integer."""
    if value is None:
        return None
    return _integer(value, min_val=min_val, max_val=max_val)


def _parse_sheet_props(props: Mapping[str, Any]) -> SheetSummary:
    """Parse sheet provider properties."""
    grid_props = _mapping(props.get('gridProperties') or {})
    sheet_id = _integer(props.get('sheetId', 0))
    title = _text(props.get('title', ''))
    index = _integer(props.get('index', 0))
    sheet_type = _text(props.get('sheetType', 'GRID'))
    row_count = _optional_integer(grid_props.get('rowCount'))
    column_count = _optional_integer(grid_props.get('columnCount'))
    return SheetSummary(
        sheet_id=sheet_id,
        title=title,
        index=index,
        sheet_type=sheet_type,
        row_count=row_count,
        column_count=column_count,
    )


def _scalar_cell(value: Any) -> None | bool | int | float | str:
    """Validate Sheets scalar cell."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SheetsProviderError('Sheets returned an invalid response')
        return value
    if isinstance(value, str):
        if len(value) > MAX_SHEETS_TEXT_CHARS or '\x00' in value:
            raise SheetsProviderError('Sheets returned an invalid response')
        return value
    raise SheetsProviderError('Sheets returned an invalid response')


def _validated_scalar_cell(value: Any) -> None | bool | int | float | str:
    """Validate input scalar cell."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        if len(value) > MAX_SHEETS_TEXT_CHARS or '\x00' in value:
            raise SheetsInputError('Invalid cell value')
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SheetsInputError('Invalid cell value')
        return value
    raise SheetsInputError('Invalid cell value')


def _validated_values(
    values: Sequence[Sequence[object]],
) -> tuple[tuple[object, ...], ...]:
    """Validate input cell values."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise SheetsInputError('Values must be a sequence')

    rows: list[tuple[object, ...]] = []
    cell_count = 0
    for row in values:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes):
            raise SheetsInputError('Row must be a sequence')
        row_tuple = tuple(_validated_scalar_cell(c) for c in row)
        cell_count += len(row_tuple)
        if cell_count > MAX_SHEETS_CELLS:
            raise SheetsInputError('Sheets cell limit exceeded')
        rows.append(row_tuple)

    result_rows = tuple(rows)
    payload = json.dumps(
        result_rows, separators=(',', ':'), ensure_ascii=False
    )
    if len(payload.encode('utf-8')) > MAX_SHEETS_PAYLOAD_BYTES:
        raise SheetsInputError('Sheets payload limit exceeded')
    return result_rows


def _validate_render_and_date_time_mode(
    render_mode: SheetsRenderMode,
    date_time_mode: SheetsDateTimeMode | None,
) -> None:
    """Validate Sheets render modes."""
    if render_mode == SheetsRenderMode.FORMATTED:
        if date_time_mode is not None:
            raise SheetsInputError(
                'date_time_mode cannot be used with formatted render mode'
            )
    else:
        if date_time_mode is None:
            raise SheetsInputError(
                'date_time_mode is required for '
                f'{render_mode.value} render mode'
            )


class SheetsGateway:
    """Normalize Sheets provider operations."""

    def __init__(
        self,
        store: GoogleCredentialStore,
        *,
        service_builder: ServiceBuilder = build_sheets_service,
        num_retries: int = REQUEST_RETRIES,
    ) -> None:
        """Initialize Sheets provider gateway."""
        self._store = store
        self._service_builder = service_builder
        self._num_retries = num_retries

    def service(self) -> Any:
        """Build authenticated Sheets service."""
        try:
            credentials = self._store.refresh()
            return self._service_builder(credentials)
        except SheetsError:
            raise
        except Exception:
            raise SheetsProviderError(
                'Sheets credentials are unavailable'
            ) from None

    @staticmethod
    def _http_reason(error: HttpError) -> str | None:
        """Read safe Sheets reason."""
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

        if status == 404:
            return SheetsNotFoundError('Sheets resource was not found')

        if status in {403, 429} and reason in _RATE_LIMIT_REASONS:
            return SheetsRateLimitError('Sheets is temporarily rate limited')

        if status == 429:
            return SheetsRateLimitError('Sheets is temporarily rate limited')

        if status == 403 and reason in _SCOPE_REASONS:
            return SheetsScopeError(
                'Google authorization lacks required Sheets permissions'
            )

        if status == 401:
            return SheetsProviderError('Google authorization requires renewal')

        if status == 400:
            return SheetsProviderError('Sheets rejected the request')

        if status == 403:
            return SheetsProviderError('Sheets request was forbidden')

        return SheetsProviderError('Sheets request is temporarily unavailable')

    def _execute_raw(self, request: Any) -> Any:
        """Execute raw Sheets request."""
        try:
            return request.execute(num_retries=self._num_retries)
        except HttpError as error:
            raise self._translate_http_error(error) from None
        except TransportError, TimeoutError, ConnectionError, OSError:
            raise SheetsProviderError(
                'Sheets request is temporarily unavailable'
            ) from None

    def _execute(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped Sheets request."""
        return _mapping(self._execute_raw(request))

    def _execute_write_raw(self, request: Any) -> Any:
        """Execute raw write request."""
        try:
            return request.execute(num_retries=0)
        except HttpError as error:
            raise self._translate_http_error(error) from None
        except TransportError, TimeoutError, ConnectionError, OSError:
            raise SheetsProviderError(
                'Sheets request is temporarily unavailable'
            ) from None

    def _execute_write(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped write request."""
        return _mapping(self._execute_write_raw(request))

    def get_spreadsheet(self, spreadsheet_id: str) -> SpreadsheetSummary:
        """Retrieve spreadsheet metadata summary."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        service = self.service()
        request = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            includeGridData=False,
        )
        data = self._execute(request)
        properties = _mapping(data.get('properties') or {})
        sheets_raw = _sequence(data.get('sheets') or (), limit=1000)

        sheets: list[SheetSummary] = []
        for item in sheets_raw:
            sheet_dict = _mapping(item)
            props = _mapping(sheet_dict.get('properties') or {})
            sheets.append(_parse_sheet_props(props))
        return SpreadsheetSummary(
            spreadsheet_id=_text(data.get('spreadsheetId') or spreadsheet_id),
            title=_text(properties.get('title', '')),
            locale=_optional_text(properties.get('locale')),
            time_zone=_optional_text(properties.get('timeZone')),
            url=_optional_text(data.get('spreadsheetUrl')),
            sheets=tuple(sheets),
        )

    def create_spreadsheet(
        self,
        title: str,
        *,
        locale: str | None = None,
        time_zone: str | None = None,
    ) -> SpreadsheetCreateResult:
        """Create new Google spreadsheet."""
        if (
            not isinstance(title, str)
            or not title.strip()
            or '\x00' in title
            or len(title) > MAX_SHEETS_TITLE_CHARS
        ):
            raise SheetsInputError(
                'Spreadsheet title must be 1-100 characters'
            )
        if locale is not None and (
            not isinstance(locale, str)
            or not locale.strip()
            or '\x00' in locale
            or len(locale) > MAX_SHEETS_TITLE_CHARS
        ):
            raise SheetsInputError('Invalid locale')
        if time_zone is not None and (
            not isinstance(time_zone, str)
            or not time_zone.strip()
            or '\x00' in time_zone
            or len(time_zone) > MAX_SHEETS_TITLE_CHARS
        ):
            raise SheetsInputError('Invalid time zone')

        properties: dict[str, Any] = {'title': title}
        if locale is not None:
            properties['locale'] = locale
        if time_zone is not None:
            properties['timeZone'] = time_zone

        service = self.service()
        request = service.spreadsheets().create(
            body={'properties': properties}
        )
        data = self._execute_write(request)

        spreadsheet_id_raw = data.get('spreadsheetId')
        if (
            not isinstance(spreadsheet_id_raw, str)
            or not spreadsheet_id_raw.strip()
        ):
            raise SheetsProviderError('Sheets returned an invalid response')
        res_properties = _mapping(data.get('properties') or {})
        sheets_raw = _sequence(data.get('sheets') or (), limit=1000)
        sheets: list[SheetSummary] = []
        for item in sheets_raw:
            sheet_dict = _mapping(item)
            props = _mapping(sheet_dict.get('properties') or {})
            sheets.append(_parse_sheet_props(props))

        return SpreadsheetCreateResult(
            spreadsheet_id=_text(spreadsheet_id_raw),
            title=_text(res_properties.get('title', title)),
            locale=_optional_text(res_properties.get('locale')),
            time_zone=_optional_text(res_properties.get('timeZone')),
            url=_optional_text(data.get('spreadsheetUrl')),
            sheets=tuple(sheets),
        )

    def add_sheet(
        self,
        spreadsheet_id: str,
        title: str,
        *,
        row_count: int | None = None,
        column_count: int | None = None,
        index: int | None = None,
    ) -> SheetMutationResult:
        """Add sheet to spreadsheet."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')
        if (
            not isinstance(title, str)
            or not title.strip()
            or '\x00' in title
            or len(title) > MAX_SHEETS_TITLE_CHARS
        ):
            raise SheetsInputError('Sheet title must be 1-100 characters')
        if (row_count is None) != (column_count is None):
            raise SheetsInputError(
                'row_count and column_count must be provided together'
            )
        if row_count is not None and column_count is not None:
            if (
                isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count < 1
                or row_count > MAX_SHEETS_GRID_CELLS
            ):
                raise SheetsInputError(
                    f'row_count must be between 1 and {MAX_SHEETS_GRID_CELLS}'
                )
            if (
                isinstance(column_count, bool)
                or not isinstance(column_count, int)
                or column_count < 1
                or column_count > MAX_SHEETS_GRID_CELLS
            ):
                raise SheetsInputError(
                    'column_count must be between 1 and '
                    f'{MAX_SHEETS_GRID_CELLS}'
                )
            if row_count * column_count > MAX_SHEETS_GRID_CELLS:
                raise SheetsInputError(
                    f'Total grid cells cannot exceed {MAX_SHEETS_GRID_CELLS}'
                )
        if index is not None and (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index > MAX_SHEETS_CELLS
        ):
            raise SheetsInputError(
                f'index must be between 0 and {MAX_SHEETS_CELLS}'
            )

        sheet_props: dict[str, Any] = {'title': title}
        grid_props: dict[str, Any] = {}
        if row_count is not None:
            grid_props['rowCount'] = row_count
        if column_count is not None:
            grid_props['columnCount'] = column_count
        if grid_props:
            sheet_props['gridProperties'] = grid_props
        if index is not None:
            sheet_props['index'] = index

        body = {
            'requests': [
                {
                    'addSheet': {
                        'properties': sheet_props,
                    }
                }
            ]
        }
        service = self.service()
        request = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body,
        )
        data = self._execute_write(request)

        replies = _sequence(data.get('replies') or (), limit=10)
        if not replies:
            raise SheetsProviderError('Sheets returned an invalid response')
        reply = _mapping(replies[0])
        add_sheet_reply = _mapping(reply.get('addSheet') or {})
        props_raw = add_sheet_reply.get('properties')
        if not isinstance(props_raw, Mapping):
            raise SheetsProviderError('Sheets returned an invalid response')
        props = _mapping(props_raw)

        summary = _parse_sheet_props(props)
        return SheetMutationResult(
            spreadsheet_id=spreadsheet_id,
            sheet_id=summary.sheet_id,
            title=summary.title,
            index=summary.index,
            sheet_type=summary.sheet_type,
            row_count=summary.row_count,
            column_count=summary.column_count,
        )

    def rename_sheet(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        new_title: str,
    ) -> SheetMutationResult:
        """Rename spreadsheet sheet tab."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')
        if (
            isinstance(sheet_id, bool)
            or not isinstance(sheet_id, int)
            or sheet_id < 0
        ):
            raise SheetsInputError('Sheet ID must be a non-negative integer')
        if (
            not isinstance(new_title, str)
            or not new_title.strip()
            or '\x00' in new_title
            or len(new_title) > MAX_SHEETS_TITLE_CHARS
        ):
            raise SheetsInputError('Sheet title must be 1-100 characters')

        body = {
            'requests': [
                {
                    'updateSheetProperties': {
                        'properties': {
                            'sheetId': sheet_id,
                            'title': new_title,
                        },
                        'fields': 'title',
                    }
                }
            ]
        }
        service = self.service()
        request = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body,
        )
        data = self._execute_write(request)

        replies = _sequence(data.get('replies') or (), limit=10)
        if not replies:
            raise SheetsProviderError('Sheets returned an invalid response')
        reply = _mapping(replies[0])
        update_reply = _mapping(reply.get('updateSheetProperties') or {})
        props_raw = update_reply.get('properties')
        if not isinstance(props_raw, Mapping):
            raise SheetsProviderError('Sheets returned an invalid response')
        props = _mapping(props_raw)

        summary = _parse_sheet_props(props)
        return SheetMutationResult(
            spreadsheet_id=spreadsheet_id,
            sheet_id=summary.sheet_id,
            title=summary.title,
            index=summary.index,
            sheet_type=summary.sheet_type,
            row_count=summary.row_count,
            column_count=summary.column_count,
        )

    def copy_sheet(
        self,
        source_spreadsheet_id: str,
        sheet_id: int,
        destination_spreadsheet_id: str,
        *,
        new_title: str | None = None,
    ) -> SheetCopyResult:
        """Copy sheet across spreadsheets."""
        if (
            not isinstance(source_spreadsheet_id, str)
            or not source_spreadsheet_id.strip()
            or '\x00' in source_spreadsheet_id
        ):
            raise SheetsInputError('Source spreadsheet ID is required')
        if (
            isinstance(sheet_id, bool)
            or not isinstance(sheet_id, int)
            or sheet_id < 0
        ):
            raise SheetsInputError('Sheet ID must be a non-negative integer')
        if (
            not isinstance(destination_spreadsheet_id, str)
            or not destination_spreadsheet_id.strip()
            or '\x00' in destination_spreadsheet_id
        ):
            raise SheetsInputError('Destination spreadsheet ID is required')
        if new_title is not None and (
            not isinstance(new_title, str)
            or not new_title.strip()
            or '\x00' in new_title
            or len(new_title) > MAX_SHEETS_TITLE_CHARS
        ):
            raise SheetsInputError('Sheet title must be 1-100 characters')

        if (
            source_spreadsheet_id != destination_spreadsheet_id
            and new_title is not None
        ):
            raise SheetsInputError(
                'new_title is only supported for same-spreadsheet copy'
            )

        service = self.service()
        if source_spreadsheet_id == destination_spreadsheet_id:
            dup_request: dict[str, Any] = {'sourceSheetId': sheet_id}
            if new_title is not None:
                dup_request['newSheetName'] = new_title

            batch_body = {
                'requests': [
                    {
                        'duplicateSheet': dup_request,
                    }
                ]
            }
            request = service.spreadsheets().batchUpdate(
                spreadsheetId=source_spreadsheet_id,
                body=batch_body,
            )
            data = self._execute_write(request)
            replies = _sequence(data.get('replies') or (), limit=10)
            if not replies:
                raise SheetsProviderError(
                    'Sheets returned an invalid response'
                )
            reply = _mapping(replies[0])
            dup_reply = _mapping(reply.get('duplicateSheet') or {})
            props_raw = dup_reply.get('properties')
            if not isinstance(props_raw, Mapping):
                raise SheetsProviderError(
                    'Sheets returned an invalid response'
                )
            props = _mapping(props_raw)
        else:
            copy_body = {
                'destinationSpreadsheetId': destination_spreadsheet_id
            }
            request = (
                service.spreadsheets()
                .sheets()
                .copyTo(
                    spreadsheetId=source_spreadsheet_id,
                    sheetId=sheet_id,
                    body=copy_body,
                )
            )
            data = self._execute_write(request)
            props = _mapping(data)
            if 'sheetId' not in props:
                raise SheetsProviderError(
                    'Sheets returned an invalid response'
                )

        summary = _parse_sheet_props(props)
        return SheetCopyResult(
            source_spreadsheet_id=source_spreadsheet_id,
            destination_spreadsheet_id=destination_spreadsheet_id,
            sheet_id=summary.sheet_id,
            title=summary.title,
            index=summary.index,
            sheet_type=summary.sheet_type,
            row_count=summary.row_count,
            column_count=summary.column_count,
        )

    def _parse_value_range(
        self,
        requested_range: str,
        data: Mapping[str, Any],
        major_dimension: MajorDimension,
    ) -> SheetsValueRange:
        """Parse provider value range."""
        resolved_range = _text(data.get('range') or requested_range)
        raw_values = data.get('values')
        if raw_values is None:
            values: tuple[tuple[object, ...], ...] = ()
        else:
            rows_seq = _sequence(raw_values, limit=MAX_SHEETS_CELLS)
            parsed_rows: list[tuple[object, ...]] = []
            for row in rows_seq:
                if not isinstance(row, Sequence) or isinstance(
                    row, str | bytes
                ):
                    raise SheetsProviderError(
                        'Sheets returned an invalid response'
                    )
                parsed_rows.append(tuple(_scalar_cell(c) for c in row))
            values = tuple(parsed_rows)

        if major_dimension == MajorDimension.ROWS:
            row_count = len(values)
            column_count = max((len(r) for r in values), default=0)
        else:
            column_count = len(values)
            row_count = max((len(c) for c in values), default=0)

        cell_count = sum(len(r) for r in values)
        if cell_count > MAX_SHEETS_CELLS:
            raise SheetsProviderError('Response exceeded maximum cell count')

        return SheetsValueRange(
            requested_range=requested_range,
            resolved_range=resolved_range,
            major_dimension=major_dimension,
            values=values,
            row_count=row_count,
            column_count=column_count,
            cell_count=cell_count,
        )

    def read_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        *,
        render_mode: SheetsRenderMode,
        date_time_mode: SheetsDateTimeMode | None = None,
        major_dimension: MajorDimension = MajorDimension.ROWS,
    ) -> SheetsValueRange:
        """Read single value range."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        validated_range = validate_a1_range(range_name)
        _validate_render_and_date_time_mode(render_mode, date_time_mode)
        kwargs: dict[str, Any] = {
            'spreadsheetId': spreadsheet_id,
            'range': validated_range,
            'valueRenderOption': _VALUE_RENDER_OPTIONS[render_mode],
            'majorDimension': _MAJOR_DIMENSIONS[major_dimension],
        }
        if date_time_mode is not None:
            kwargs['dateTimeRenderOption'] = _DATE_TIME_RENDER_OPTIONS[
                date_time_mode
            ]

        service = self.service()
        request = service.spreadsheets().values().get(**kwargs)
        data = self._execute(request)
        return self._parse_value_range(validated_range, data, major_dimension)

    def batch_read_ranges(
        self,
        spreadsheet_id: str,
        ranges: Sequence[str],
        *,
        render_mode: SheetsRenderMode,
        date_time_mode: SheetsDateTimeMode | None = None,
        major_dimension: MajorDimension = MajorDimension.ROWS,
    ) -> SheetsBatchReadResult:
        """Read multiple value ranges."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        if isinstance(ranges, str | bytes):
            raise SheetsInputError(
                'Ranges must be a sequence of range strings'
            )

        if not ranges:
            raise SheetsInputError('At least one range is required')

        if len(ranges) > MAX_SHEETS_RANGES:
            raise SheetsInputError(
                f'Cannot request more than {MAX_SHEETS_RANGES} ranges'
            )

        validated_ranges = [validate_a1_range(r) for r in ranges]
        _validate_render_and_date_time_mode(render_mode, date_time_mode)
        kwargs: dict[str, Any] = {
            'spreadsheetId': spreadsheet_id,
            'ranges': validated_ranges,
            'valueRenderOption': _VALUE_RENDER_OPTIONS[render_mode],
            'majorDimension': _MAJOR_DIMENSIONS[major_dimension],
        }
        if date_time_mode is not None:
            kwargs['dateTimeRenderOption'] = _DATE_TIME_RENDER_OPTIONS[
                date_time_mode
            ]

        service = self.service()
        request = service.spreadsheets().values().batchGet(**kwargs)
        data = self._execute(request)
        raw_value_ranges = _sequence(
            data.get('valueRanges') or (), limit=MAX_SHEETS_RANGES
        )
        if len(raw_value_ranges) != len(validated_ranges):
            raise SheetsProviderError('Sheets returned an invalid response')

        parsed_ranges: list[SheetsValueRange] = []
        total_cells = 0
        for req_range, vr_data in zip(validated_ranges, raw_value_ranges):
            vr_dict = _mapping(vr_data)
            val_range = self._parse_value_range(
                req_range, vr_dict, major_dimension
            )
            total_cells += val_range.cell_count
            if total_cells > MAX_SHEETS_CELLS:
                raise SheetsProviderError(
                    'Response exceeded maximum cell count'
                )
            parsed_ranges.append(val_range)

        return SheetsBatchReadResult(
            spreadsheet_id=_text(data.get('spreadsheetId') or spreadsheet_id),
            ranges=tuple(parsed_ranges),
        )

    def _parse_write_result(
        self, spreadsheet_id: str, data: Mapping[str, Any]
    ) -> SheetsWriteResult:
        """Parse write response result."""
        resp_id = _text(data.get('spreadsheetId') or spreadsheet_id)
        raw_range = data.get('updatedRange')
        if (
            not isinstance(raw_range, str)
            or not raw_range.strip()
            or '\x00' in raw_range
            or len(raw_range) > MAX_SHEETS_TEXT_CHARS
        ):
            raise SheetsProviderError('Sheets returned an invalid response')
        updated_range = raw_range
        updated_rows = _integer(data.get('updatedRows', 0))
        updated_columns = _integer(data.get('updatedColumns', 0))
        updated_cells = _integer(data.get('updatedCells', 0))
        return SheetsWriteResult(
            spreadsheet_id=resp_id,
            updated_range=updated_range,
            updated_rows=updated_rows,
            updated_columns=updated_columns,
            updated_cells=updated_cells,
        )

    def _parse_append_result(
        self, spreadsheet_id: str, data: Mapping[str, Any]
    ) -> SheetsAppendResult:
        """Parse append response result."""
        resp_id = _text(data.get('spreadsheetId') or spreadsheet_id)
        table_range = _optional_text(data.get('tableRange'))
        if table_range == '':
            table_range = None
        updates_raw = data.get('updates')
        if updates_raw is None:
            raise SheetsProviderError('Sheets returned an invalid response')
        updates = _mapping(updates_raw)
        write_res = self._parse_write_result(resp_id, updates)
        return SheetsAppendResult(
            spreadsheet_id=resp_id,
            table_range=table_range,
            updated_range=write_res.updated_range,
            updated_rows=write_res.updated_rows,
            updated_columns=write_res.updated_columns,
            updated_cells=write_res.updated_cells,
        )

    def _parse_batch_write_result(
        self,
        spreadsheet_id: str,
        data: Mapping[str, Any],
        expected_count: int,
    ) -> SheetsBatchWriteResult:
        """Parse batch write response."""
        resp_id = _text(data.get('spreadsheetId') or spreadsheet_id)
        total_updated_rows = _integer(data.get('totalUpdatedRows', 0))
        total_updated_columns = _integer(data.get('totalUpdatedColumns', 0))
        total_updated_cells = _integer(data.get('totalUpdatedCells', 0))
        total_updated_sheets = _integer(data.get('totalUpdatedSheets', 0))
        responses_raw = data.get('responses')
        if responses_raw is None:
            raise SheetsProviderError('Sheets returned an invalid response')
        responses_seq = _sequence(responses_raw, limit=MAX_SHEETS_RANGES)
        if len(responses_seq) != expected_count:
            raise SheetsProviderError('Sheets returned an invalid response')
        responses: list[SheetsWriteResult] = []
        for r_data in responses_seq:
            r_dict = _mapping(r_data)
            responses.append(self._parse_write_result(resp_id, r_dict))
        return SheetsBatchWriteResult(
            spreadsheet_id=resp_id,
            total_updated_rows=total_updated_rows,
            total_updated_columns=total_updated_columns,
            total_updated_cells=total_updated_cells,
            total_updated_sheets=total_updated_sheets,
            responses=tuple(responses),
        )

    def _parse_clear_result(
        self,
        spreadsheet_id: str,
        data: Mapping[str, Any],
        expected_count: int,
    ) -> SheetsClearResult:
        """Parse clear response result."""
        resp_id = _text(data.get('spreadsheetId') or spreadsheet_id)
        cleared_raw = data.get('clearedRanges')
        if cleared_raw is None:
            raise SheetsProviderError('Sheets returned an invalid response')
        cleared_seq = _sequence(cleared_raw, limit=MAX_SHEETS_RANGES)
        if len(cleared_seq) != expected_count:
            raise SheetsProviderError('Sheets returned an invalid response')
        cleared = tuple(_text(r) for r in cleared_seq)
        return SheetsClearResult(
            spreadsheet_id=resp_id,
            cleared_ranges=cleared,
        )

    def update_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: Sequence[Sequence[object]],
        *,
        input_mode: SheetsInputMode,
    ) -> SheetsWriteResult:
        """Write values to range."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        validated_range = validate_a1_range(range_name)
        validated_rows = _validated_values(values)
        body = {'values': [list(row) for row in validated_rows]}
        kwargs: dict[str, Any] = {
            'spreadsheetId': spreadsheet_id,
            'range': validated_range,
            'valueInputOption': _VALUE_INPUT_OPTIONS[input_mode],
            'includeValuesInResponse': False,
            'body': body,
        }

        service = self.service()
        request = service.spreadsheets().values().update(**kwargs)
        data = self._execute_write(request)
        return self._parse_write_result(spreadsheet_id, data)

    def append_rows(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: Sequence[Sequence[object]],
        *,
        input_mode: SheetsInputMode,
        insert_mode: SheetsInsertMode,
    ) -> SheetsAppendResult:
        """Append rows to table."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        validated_range = validate_a1_range(range_name)
        validated_rows = _validated_values(values)
        body = {'values': [list(row) for row in validated_rows]}
        kwargs: dict[str, Any] = {
            'spreadsheetId': spreadsheet_id,
            'range': validated_range,
            'valueInputOption': _VALUE_INPUT_OPTIONS[input_mode],
            'insertDataOption': _INSERT_DATA_OPTIONS[insert_mode],
            'includeValuesInResponse': False,
            'body': body,
        }

        service = self.service()
        request = service.spreadsheets().values().append(**kwargs)
        data = self._execute_write(request)
        return self._parse_append_result(spreadsheet_id, data)

    def batch_update_ranges(
        self,
        spreadsheet_id: str,
        data: Sequence[SheetsWriteRange],
        *,
        input_mode: SheetsInputMode,
    ) -> SheetsBatchWriteResult:
        """Write values to ranges."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        if not isinstance(data, Sequence) or isinstance(data, str | bytes):
            raise SheetsInputError('Data must be a sequence')

        if not data:
            raise SheetsInputError('At least one range is required')

        if len(data) > MAX_SHEETS_RANGES:
            raise SheetsInputError(
                f'Cannot request more than {MAX_SHEETS_RANGES} ranges'
            )

        data_entries: list[dict[str, Any]] = []
        total_cells = 0
        for item in data:
            if not isinstance(item, SheetsWriteRange):
                raise SheetsInputError('Invalid write range item')
            val_range = validate_a1_range(item.range_name)
            val_rows = _validated_values(item.values)
            total_cells += sum(len(r) for r in val_rows)
            if total_cells > MAX_SHEETS_CELLS:
                raise SheetsInputError('Sheets cell limit exceeded')
            data_entries.append(
                {
                    'range': val_range,
                    'majorDimension': 'ROWS',
                    'values': [list(r) for r in val_rows],
                }
            )

        batch_payload: dict[str, Any] = {
            'valueInputOption': _VALUE_INPUT_OPTIONS[input_mode],
            'data': data_entries,
            'includeValuesInResponse': False,
        }
        encoded_payload = json.dumps(
            batch_payload, separators=(',', ':'), ensure_ascii=False
        ).encode('utf-8')
        if len(encoded_payload) > MAX_SHEETS_PAYLOAD_BYTES:
            raise SheetsInputError('Sheets payload limit exceeded')

        kwargs: dict[str, Any] = {
            'spreadsheetId': spreadsheet_id,
            'body': batch_payload,
        }

        service = self.service()
        request = service.spreadsheets().values().batchUpdate(**kwargs)
        resp_data = self._execute_write(request)
        return self._parse_batch_write_result(
            spreadsheet_id, resp_data, len(data_entries)
        )

    def clear_ranges(
        self,
        spreadsheet_id: str,
        ranges: Sequence[str],
    ) -> SheetsClearResult:
        """Clear values from ranges."""
        if (
            not isinstance(spreadsheet_id, str)
            or not spreadsheet_id.strip()
            or '\x00' in spreadsheet_id
        ):
            raise SheetsInputError('Spreadsheet ID is required')

        if not isinstance(ranges, Sequence) or isinstance(ranges, str | bytes):
            raise SheetsInputError(
                'Ranges must be a sequence of range strings'
            )
        if not ranges:
            raise SheetsInputError('At least one range is required')

        if len(ranges) > MAX_SHEETS_RANGES:
            raise SheetsInputError(
                f'Cannot request more than {MAX_SHEETS_RANGES} ranges'
            )

        validated_ranges = [validate_a1_range(r) for r in ranges]
        kwargs: dict[str, Any] = {
            'spreadsheetId': spreadsheet_id,
            'body': {'ranges': validated_ranges},
        }

        service = self.service()
        request = service.spreadsheets().values().batchClear(**kwargs)
        resp_data = self._execute_write(request)
        return self._parse_clear_result(
            spreadsheet_id, resp_data, len(validated_ranges)
        )
