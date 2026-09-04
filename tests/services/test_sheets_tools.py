"""Test Sheets MCP tools."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.sheets.constants import (
    MAX_SHEETS_RANGES,
    MAX_SHEETS_TITLE_CHARS,
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
from google_workspace_mcp.services.sheets.tools import register_sheets_tools
from google_workspace_mcp.services.sheets.tools.common import run_gateway
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

SHEETS_TOOL_NAMES = {
    'sheets_get_spreadsheet',
    'sheets_read_range',
    'sheets_batch_read_ranges',
    'sheets_update_range',
    'sheets_append_rows',
    'sheets_batch_update_ranges',
    'sheets_clear_ranges',
    'sheets_create_spreadsheet',
    'sheets_add_sheet',
    'sheets_rename_sheet',
    'sheets_copy_sheet',
}

READONLY_SHEETS_TOOLS = {
    'sheets_get_spreadsheet',
    'sheets_read_range',
    'sheets_batch_read_ranges',
}


class FakeGateway:
    """Record Sheets gateway calls."""

    def __init__(self) -> None:
        """Initialize fake gateway tracker."""
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}

    def get_spreadsheet(self, spreadsheet_id: str) -> SpreadsheetSummary:
        """Record get_spreadsheet call."""
        self.calls.append(('get_spreadsheet', (spreadsheet_id,), {}))
        if 'get_spreadsheet' in self.errors:
            raise self.errors['get_spreadsheet']
        return self.responses.get(
            'get_spreadsheet',
            SpreadsheetSummary(
                spreadsheet_id=spreadsheet_id,
                title='Test Spreadsheet',
                locale='en_US',
                time_zone='UTC',
                url='https://docs.google.com/spreadsheets/d/' + spreadsheet_id,
                sheets=(
                    SheetSummary(
                        sheet_id=0,
                        title='Sheet1',
                        index=0,
                        sheet_type='GRID',
                        row_count=100,
                        column_count=26,
                    ),
                ),
            ),
        )

    def read_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        *,
        render_mode: SheetsRenderMode,
        date_time_mode: SheetsDateTimeMode | None = None,
    ) -> SheetsValueRange:
        """Record read_range call."""
        self.calls.append(
            (
                'read_range',
                (spreadsheet_id, range_name),
                {'render_mode': render_mode, 'date_time_mode': date_time_mode},
            )
        )
        if 'read_range' in self.errors:
            raise self.errors['read_range']
        return self.responses.get(
            'read_range',
            SheetsValueRange(
                requested_range=range_name,
                resolved_range=range_name,
                major_dimension=MajorDimension.ROWS,
                values=((1, 2), (3, 4)),
                row_count=2,
                column_count=2,
                cell_count=4,
            ),
        )

    def batch_read_ranges(
        self,
        spreadsheet_id: str,
        ranges: list[str],
        *,
        render_mode: SheetsRenderMode,
        date_time_mode: SheetsDateTimeMode | None = None,
    ) -> SheetsBatchReadResult:
        """Record batch_read_ranges call."""
        self.calls.append(
            (
                'batch_read_ranges',
                (spreadsheet_id, ranges),
                {'render_mode': render_mode, 'date_time_mode': date_time_mode},
            )
        )
        if 'batch_read_ranges' in self.errors:
            raise self.errors['batch_read_ranges']
        return self.responses.get(
            'batch_read_ranges',
            SheetsBatchReadResult(
                spreadsheet_id=spreadsheet_id,
                ranges=(
                    SheetsValueRange(
                        requested_range='Sheet1!A1:B2',
                        resolved_range='Sheet1!A1:B2',
                        major_dimension=MajorDimension.ROWS,
                        values=((1, 2),),
                        row_count=1,
                        column_count=2,
                        cell_count=2,
                    ),
                ),
            ),
        )

    def update_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[object]],
        *,
        input_mode: SheetsInputMode,
    ) -> SheetsWriteResult:
        """Record update_range call."""
        self.calls.append(
            (
                'update_range',
                (spreadsheet_id, range_name, values),
                {'input_mode': input_mode},
            )
        )
        if 'update_range' in self.errors:
            raise self.errors['update_range']
        return self.responses.get(
            'update_range',
            SheetsWriteResult(
                spreadsheet_id=spreadsheet_id,
                updated_range=range_name,
                updated_rows=len(values),
                updated_columns=len(values[0]) if values else 0,
                updated_cells=len(values) * (len(values[0]) if values else 0),
            ),
        )

    def append_rows(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[object]],
        *,
        input_mode: SheetsInputMode,
        insert_mode: SheetsInsertMode,
    ) -> SheetsAppendResult:
        """Record append_rows call."""
        self.calls.append(
            (
                'append_rows',
                (spreadsheet_id, range_name, values),
                {'input_mode': input_mode, 'insert_mode': insert_mode},
            )
        )
        if 'append_rows' in self.errors:
            raise self.errors['append_rows']
        return self.responses.get(
            'append_rows',
            SheetsAppendResult(
                spreadsheet_id=spreadsheet_id,
                table_range=range_name,
                updated_range='Sheet1!A3:B4',
                updated_rows=len(values),
                updated_columns=len(values[0]) if values else 0,
                updated_cells=len(values) * (len(values[0]) if values else 0),
            ),
        )

    def batch_update_ranges(
        self,
        spreadsheet_id: str,
        data: list[SheetsWriteRange],
        *,
        input_mode: SheetsInputMode,
    ) -> SheetsBatchWriteResult:
        """Record batch_update_ranges call."""
        self.calls.append(
            (
                'batch_update_ranges',
                (spreadsheet_id, data),
                {'input_mode': input_mode},
            )
        )
        if 'batch_update_ranges' in self.errors:
            raise self.errors['batch_update_ranges']
        return self.responses.get(
            'batch_update_ranges',
            SheetsBatchWriteResult(
                spreadsheet_id=spreadsheet_id,
                total_updated_rows=2,
                total_updated_columns=2,
                total_updated_cells=4,
                total_updated_sheets=1,
                responses=(
                    SheetsWriteResult(
                        spreadsheet_id=spreadsheet_id,
                        updated_range='Sheet1!A1:B1',
                        updated_rows=1,
                        updated_columns=2,
                        updated_cells=2,
                    ),
                ),
            ),
        )

    def clear_ranges(
        self,
        spreadsheet_id: str,
        ranges: list[str],
    ) -> SheetsClearResult:
        """Record clear_ranges call."""
        self.calls.append(('clear_ranges', (spreadsheet_id, ranges), {}))
        if 'clear_ranges' in self.errors:
            raise self.errors['clear_ranges']
        return self.responses.get(
            'clear_ranges',
            SheetsClearResult(
                spreadsheet_id=spreadsheet_id,
                cleared_ranges=tuple(ranges),
            ),
        )

    def create_spreadsheet(
        self,
        title: str,
        *,
        locale: str | None = None,
        time_zone: str | None = None,
    ) -> SpreadsheetCreateResult:
        """Record create_spreadsheet call."""
        self.calls.append(
            (
                'create_spreadsheet',
                (title,),
                {'locale': locale, 'time_zone': time_zone},
            )
        )
        if 'create_spreadsheet' in self.errors:
            raise self.errors['create_spreadsheet']
        return self.responses.get(
            'create_spreadsheet',
            SpreadsheetCreateResult(
                spreadsheet_id='new-book-1',
                title=title,
                locale=locale,
                time_zone=time_zone,
                url='https://docs.google.com/spreadsheets/d/new-book-1/edit',
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
            ),
        )

    def add_sheet(
        self,
        spreadsheet_id: str,
        title: str,
        *,
        row_count: int | None = None,
        column_count: int | None = None,
    ) -> SheetMutationResult:
        """Record add_sheet call."""
        self.calls.append(
            (
                'add_sheet',
                (spreadsheet_id, title),
                {'row_count': row_count, 'column_count': column_count},
            )
        )
        if 'add_sheet' in self.errors:
            raise self.errors['add_sheet']
        return self.responses.get(
            'add_sheet',
            SheetMutationResult(
                spreadsheet_id=spreadsheet_id,
                sheet_id=12345,
                title=title,
                index=1,
                sheet_type='GRID',
                row_count=row_count or 1000,
                column_count=column_count or 26,
            ),
        )

    def rename_sheet(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        title: str,
    ) -> SheetMutationResult:
        """Record rename_sheet call."""
        self.calls.append(
            ('rename_sheet', (spreadsheet_id, sheet_id, title), {})
        )
        if 'rename_sheet' in self.errors:
            raise self.errors['rename_sheet']
        return self.responses.get(
            'rename_sheet',
            SheetMutationResult(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                title=title,
                index=0,
                sheet_type='GRID',
                row_count=100,
                column_count=26,
            ),
        )

    def copy_sheet(
        self,
        source_spreadsheet_id: str,
        sheet_id: int,
        destination_spreadsheet_id: str,
        *,
        new_title: str | None = None,
    ) -> SheetCopyResult:
        """Record copy_sheet call."""
        self.calls.append(
            (
                'copy_sheet',
                (source_spreadsheet_id, sheet_id, destination_spreadsheet_id),
                {'new_title': new_title},
            )
        )
        if 'copy_sheet' in self.errors:
            raise self.errors['copy_sheet']
        return self.responses.get(
            'copy_sheet',
            SheetCopyResult(
                source_spreadsheet_id=source_spreadsheet_id,
                destination_spreadsheet_id=destination_spreadsheet_id,
                sheet_id=99999,
                title=new_title or 'Copy of Sheet1',
                index=2,
                sheet_type='GRID',
                row_count=100,
                column_count=26,
            ),
        )


def _set_full_principal() -> Any:
    """Set full access principal."""
    principal = context.AuthenticatedPrincipal(
        principal_id='full_principal',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    return context.set_request_context(principal, 'request')


@pytest.mark.asyncio
async def test_registers_exact_sheets_inventory() -> None:
    server = PolicyMCPServer('sheets')
    gateway = FakeGateway()
    register_sheets_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    token = _set_full_principal()
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)

    assert {tool.name for tool in tools} == SHEETS_TOOL_NAMES
    assert set(server.readonly_capabilities()) == READONLY_SHEETS_TOOLS

    for tool in tools:
        assert tool.output_schema is not None

    public = {tool.name: tool for tool in tools}

    read_range = public['sheets_read_range'].input_schema
    assert set(read_range['properties']) == {
        'spreadsheet_id',
        'range_name',
        'render_mode',
        'date_time_mode',
    }
    assert set(read_range['required']) == {
        'spreadsheet_id',
        'range_name',
        'render_mode',
    }

    batch_read = public['sheets_batch_read_ranges'].input_schema
    assert set(batch_read['properties']) == {
        'spreadsheet_id',
        'ranges',
        'render_mode',
        'date_time_mode',
    }
    assert set(batch_read['required']) == {
        'spreadsheet_id',
        'ranges',
        'render_mode',
    }
    assert batch_read['properties']['ranges']['minItems'] == 1
    assert batch_read['properties']['ranges']['maxItems'] == MAX_SHEETS_RANGES

    update_range = public['sheets_update_range'].input_schema
    assert set(update_range['properties']) == {
        'spreadsheet_id',
        'range_name',
        'values',
        'input_mode',
    }
    assert set(update_range['required']) == {
        'spreadsheet_id',
        'range_name',
        'values',
        'input_mode',
    }

    append_rows = public['sheets_append_rows'].input_schema
    assert set(append_rows['properties']) == {
        'spreadsheet_id',
        'range_name',
        'values',
        'input_mode',
        'insert_mode',
    }
    assert set(append_rows['required']) == {
        'spreadsheet_id',
        'range_name',
        'values',
        'input_mode',
        'insert_mode',
    }

    batch_update = public['sheets_batch_update_ranges'].input_schema
    assert set(batch_update['properties']) == {
        'spreadsheet_id',
        'data',
        'input_mode',
    }
    assert set(batch_update['required']) == {
        'spreadsheet_id',
        'data',
        'input_mode',
    }
    assert batch_update['properties']['data']['minItems'] == 1
    assert batch_update['properties']['data']['maxItems'] == MAX_SHEETS_RANGES

    clear_ranges = public['sheets_clear_ranges'].input_schema
    assert set(clear_ranges['properties']) == {'spreadsheet_id', 'ranges'}
    assert set(clear_ranges['required']) == {'spreadsheet_id', 'ranges'}
    assert clear_ranges['properties']['ranges']['minItems'] == 1
    assert (
        clear_ranges['properties']['ranges']['maxItems'] == MAX_SHEETS_RANGES
    )

    create_ss = public['sheets_create_spreadsheet'].input_schema
    assert set(create_ss['properties']) == {'title', 'locale', 'time_zone'}
    assert set(create_ss['required']) == {'title'}
    assert create_ss['properties']['title']['minLength'] == 1
    assert (
        create_ss['properties']['title']['maxLength'] == MAX_SHEETS_TITLE_CHARS
    )

    add_sheet = public['sheets_add_sheet'].input_schema
    assert set(add_sheet['properties']) == {
        'spreadsheet_id',
        'title',
        'row_count',
        'column_count',
    }
    assert set(add_sheet['required']) == {'spreadsheet_id', 'title'}
    assert add_sheet['properties']['title']['minLength'] == 1
    assert (
        add_sheet['properties']['title']['maxLength'] == MAX_SHEETS_TITLE_CHARS
    )

    rename_sheet = public['sheets_rename_sheet'].input_schema
    assert set(rename_sheet['properties']) == {
        'spreadsheet_id',
        'sheet_id',
        'title',
    }
    assert set(rename_sheet['required']) == {
        'spreadsheet_id',
        'sheet_id',
        'title',
    }
    assert rename_sheet['properties']['sheet_id']['minimum'] == 0
    assert (
        rename_sheet['properties']['title']['maxLength']
        == MAX_SHEETS_TITLE_CHARS
    )

    copy_sheet = public['sheets_copy_sheet'].input_schema
    assert set(copy_sheet['properties']) == {
        'source_spreadsheet_id',
        'sheet_id',
        'destination_spreadsheet_id',
        'title',
    }
    assert set(copy_sheet['required']) == {
        'source_spreadsheet_id',
        'sheet_id',
    }
    assert copy_sheet['properties']['sheet_id']['minimum'] == 0

    prohibited_names = (
        'delete_spreadsheet',
        'delete_sheet',
        'share_spreadsheet',
        'set_permissions',
        'download_sheet',
    )
    for tool_name in SHEETS_TOOL_NAMES:
        for prohibited in prohibited_names:
            assert prohibited not in tool_name


def test_sheets_annotations_match_design() -> None:
    server = PolicyMCPServer('sheets')
    gateway = FakeGateway()
    register_sheets_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    for tool in server._tool_manager.list_tools():
        assert tool.annotations is not None

    by_name = {
        tool.name: tool.annotations
        for tool in server._tool_manager.list_tools()
        if tool.annotations is not None
    }

    for name in READONLY_SHEETS_TOOLS:
        ann = by_name[name]
        assert ann.read_only_hint is True
        assert ann.destructive_hint is False
        assert ann.idempotent_hint is True
        assert ann.open_world_hint is True

    ann = by_name['sheets_update_range']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is True
    assert ann.open_world_hint is True

    ann = by_name['sheets_append_rows']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False
    assert ann.open_world_hint is True

    ann = by_name['sheets_batch_update_ranges']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is True
    assert ann.open_world_hint is True

    ann = by_name['sheets_clear_ranges']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is True
    assert ann.idempotent_hint is True
    assert ann.open_world_hint is True

    ann = by_name['sheets_create_spreadsheet']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False
    assert ann.open_world_hint is True

    ann = by_name['sheets_add_sheet']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False
    assert ann.open_world_hint is True

    ann = by_name['sheets_rename_sheet']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is True
    assert ann.open_world_hint is True

    ann = by_name['sheets_copy_sheet']
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False
    assert ann.open_world_hint is True


@pytest.mark.asyncio
async def test_sheets_tools_forward_to_gateway() -> None:
    server = PolicyMCPServer('sheets')
    gateway = FakeGateway()
    register_sheets_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    token = _set_full_principal()
    try:
        # Step 1: get_spreadsheet
        result = await server.call_tool(
            'sheets_get_spreadsheet', {'spreadsheet_id': 'book-1'}
        )
        assert result.structured_content['spreadsheet_id'] == 'book-1'
        assert result.structured_content['title'] == 'Test Spreadsheet'
        assert result.structured_content['sheets'][0]['sheet_id'] == 0
        assert gateway.calls[-1] == ('get_spreadsheet', ('book-1',), {})

        # Step 2: read_range
        read_res = await server.call_tool(
            'sheets_read_range',
            {
                'spreadsheet_id': 'book-1',
                'range_name': 'Sheet1!A1:B2',
                'render_mode': 'unformatted',
                'date_time_mode': 'serial_number',
            },
        )
        assert read_res.structured_content['requested_range'] == 'Sheet1!A1:B2'
        assert gateway.calls[-1] == (
            'read_range',
            ('book-1', 'Sheet1!A1:B2'),
            {
                'render_mode': SheetsRenderMode.UNFORMATTED,
                'date_time_mode': SheetsDateTimeMode.SERIAL_NUMBER,
            },
        )

        # Step 3: batch_read_ranges
        batch_res = await server.call_tool(
            'sheets_batch_read_ranges',
            {
                'spreadsheet_id': 'book-1',
                'ranges': ['Sheet1!A1:B2', 'Sheet1!C1:D2'],
                'render_mode': 'formatted',
            },
        )
        assert batch_res.structured_content['spreadsheet_id'] == 'book-1'
        assert gateway.calls[-1] == (
            'batch_read_ranges',
            ('book-1', ['Sheet1!A1:B2', 'Sheet1!C1:D2']),
            {
                'render_mode': SheetsRenderMode.FORMATTED,
                'date_time_mode': None,
            },
        )

        # Step 4: update_range
        update_res = await server.call_tool(
            'sheets_update_range',
            {
                'spreadsheet_id': 'book-1',
                'range_name': 'Sheet1!A1:B2',
                'values': [[1, 2], [3, 4]],
                'input_mode': 'user_entered',
            },
        )
        assert update_res.structured_content['updated_range'] == 'Sheet1!A1:B2'
        assert gateway.calls[-1] == (
            'update_range',
            ('book-1', 'Sheet1!A1:B2', [[1, 2], [3, 4]]),
            {'input_mode': SheetsInputMode.USER_ENTERED},
        )

        # Step 5: append_rows
        append_res = await server.call_tool(
            'sheets_append_rows',
            {
                'spreadsheet_id': 'book-1',
                'range_name': 'Sheet1!A1:B2',
                'values': [['val1', 'val2']],
                'input_mode': 'raw',
                'insert_mode': 'insert_rows',
            },
        )
        assert append_res.structured_content['updated_range'] == 'Sheet1!A3:B4'
        assert gateway.calls[-1] == (
            'append_rows',
            ('book-1', 'Sheet1!A1:B2', [['val1', 'val2']]),
            {
                'input_mode': SheetsInputMode.RAW,
                'insert_mode': SheetsInsertMode.INSERT_ROWS,
            },
        )

        # Step 6: batch_update_ranges
        batch_up_res = await server.call_tool(
            'sheets_batch_update_ranges',
            {
                'spreadsheet_id': 'book-1',
                'data': [
                    {'range_name': 'Sheet1!A1:B1', 'values': [[10, 20]]},
                ],
                'input_mode': 'raw',
            },
        )
        assert batch_up_res.structured_content['total_updated_cells'] == 4
        assert gateway.calls[-1][0] == 'batch_update_ranges'
        assert gateway.calls[-1][1][0] == 'book-1'
        assert gateway.calls[-1][2] == {'input_mode': SheetsInputMode.RAW}

        # Step 7: clear_ranges
        clear_res = await server.call_tool(
            'sheets_clear_ranges',
            {
                'spreadsheet_id': 'book-1',
                'ranges': ['Sheet1!A1:B10'],
            },
        )
        assert clear_res.structured_content['cleared_ranges'] == [
            'Sheet1!A1:B10'
        ]
        assert gateway.calls[-1] == (
            'clear_ranges',
            ('book-1', ['Sheet1!A1:B10']),
            {},
        )

        # Step 8: create_spreadsheet
        create_res = await server.call_tool(
            'sheets_create_spreadsheet',
            {
                'title': 'New Spreadsheet',
                'locale': 'en_US',
                'time_zone': 'America/New_York',
            },
        )
        assert create_res.structured_content['title'] == 'New Spreadsheet'
        assert gateway.calls[-1] == (
            'create_spreadsheet',
            ('New Spreadsheet',),
            {'locale': 'en_US', 'time_zone': 'America/New_York'},
        )

        # Step 9: add_sheet
        add_res = await server.call_tool(
            'sheets_add_sheet',
            {
                'spreadsheet_id': 'book-1',
                'title': 'NewSheet',
                'row_count': 500,
                'column_count': 10,
            },
        )
        assert add_res.structured_content['title'] == 'NewSheet'
        assert gateway.calls[-1] == (
            'add_sheet',
            ('book-1', 'NewSheet'),
            {'row_count': 500, 'column_count': 10},
        )

        # Step 10: rename_sheet
        rename_res = await server.call_tool(
            'sheets_rename_sheet',
            {
                'spreadsheet_id': 'book-1',
                'sheet_id': 123,
                'title': 'RenamedSheet',
            },
        )
        assert rename_res.structured_content['title'] == 'RenamedSheet'
        assert gateway.calls[-1] == (
            'rename_sheet',
            ('book-1', 123, 'RenamedSheet'),
            {},
        )

        # Step 11: copy_sheet
        copy_res = await server.call_tool(
            'sheets_copy_sheet',
            {
                'source_spreadsheet_id': 'book-1',
                'sheet_id': 123,
                'destination_spreadsheet_id': 'book-2',
                'title': 'CopiedSheet',
            },
        )
        assert copy_res.structured_content['title'] == 'CopiedSheet'
        assert gateway.calls[-1] == (
            'copy_sheet',
            ('book-1', 123, 'book-2'),
            {
                'new_title': 'CopiedSheet',
            },
        )
    finally:
        context.reset_request_context(token)


@pytest.mark.asyncio
async def test_sheets_tools_reject_missing_required_modes() -> None:
    server = PolicyMCPServer('sheets')
    gateway = FakeGateway()
    register_sheets_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    token = _set_full_principal()
    try:
        with pytest.raises(Exception):
            await server.call_tool(
                'sheets_read_range',
                {'spreadsheet_id': 'book-1', 'range_name': 'Sheet1!A1'},
            )

        with pytest.raises(Exception):
            await server.call_tool(
                'sheets_batch_read_ranges',
                {'spreadsheet_id': 'book-1', 'ranges': ['Sheet1!A1']},
            )

        with pytest.raises(Exception):
            await server.call_tool(
                'sheets_update_range',
                {
                    'spreadsheet_id': 'book-1',
                    'range_name': 'Sheet1!A1',
                    'values': [[1]],
                },
            )

        with pytest.raises(Exception):
            await server.call_tool(
                'sheets_append_rows',
                {
                    'spreadsheet_id': 'book-1',
                    'range_name': 'Sheet1!A1',
                    'values': [[1]],
                    'insert_mode': 'insert_rows',
                },
            )

        with pytest.raises(Exception):
            await server.call_tool(
                'sheets_append_rows',
                {
                    'spreadsheet_id': 'book-1',
                    'range_name': 'Sheet1!A1',
                    'values': [[1]],
                    'input_mode': 'user_entered',
                },
            )

        with pytest.raises(Exception):
            await server.call_tool(
                'sheets_batch_update_ranges',
                {
                    'spreadsheet_id': 'book-1',
                    'data': [{'range_name': 'Sheet1!A1', 'values': [[1]]}],
                },
            )
    finally:
        context.reset_request_context(token)


@pytest.mark.asyncio
async def test_sheets_error_translation_to_tool_error() -> None:
    server = PolicyMCPServer('sheets')
    gateway = FakeGateway()
    register_sheets_tools(ToolRegistrar(server), gateway)  # type: ignore[arg-type]

    token = _set_full_principal()
    try:
        errors = [
            SheetsInputError('invalid range'),
            SheetsNotFoundError('spreadsheet not found'),
            SheetsProviderError('provider error'),
            SheetsRateLimitError('rate limit exceeded'),
            SheetsScopeError('insufficient scope'),
        ]
        for err in errors:
            gateway.errors['get_spreadsheet'] = err
            with pytest.raises(ToolError) as exc_info:
                await server.call_tool(
                    'sheets_get_spreadsheet', {'spreadsheet_id': 'book-1'}
                )
            assert str(err) in str(exc_info.value)
    finally:
        context.reset_request_context(token)


@pytest.mark.asyncio
async def test_run_gateway_only_translates_sheets_error() -> None:
    def good_fn(x: int) -> int:
        """Double input test value."""
        return x * 2

    assert await run_gateway(good_fn, 5) == 10

    def sheets_err_fn() -> None:
        """Raise simulated input error."""
        raise SheetsInputError('bad input')

    with pytest.raises(ToolError) as exc_info:
        await run_gateway(sheets_err_fn)
    assert 'bad input' in str(exc_info.value)

    def runtime_err_fn() -> None:
        """Raise simulated runtime error."""
        raise RuntimeError('unexpected boom')

    with pytest.raises(RuntimeError) as exc_info2:
        await run_gateway(runtime_err_fn)
    assert 'unexpected boom' in str(exc_info2.value)
