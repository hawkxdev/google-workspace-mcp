"""Register Sheets value tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import SheetsGateway
from ..constants import (
    MAX_SHEETS_A1_CHARS,
    MAX_SHEETS_RANGES,
    MAX_SHEETS_TEXT_CHARS,
)
from ..schemas import (
    SheetsAppendResult,
    SheetsBatchWriteResult,
    SheetsClearResult,
    SheetsInputMode,
    SheetsInsertMode,
    SheetsWriteRange,
    SheetsWriteResult,
)
from .common import run_gateway


def register_value_tools(
    registrar: ToolRegistrar,
    gateway: SheetsGateway,
) -> None:
    """Register Sheets value tools."""
    update_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
    append_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
    clear_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='sheets_update_range',
        title='Update Spreadsheet Range',
        description='Write 2D array of values to a single A1 notation range.',
        annotations=update_annotations,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_update_range(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
        range_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_A1_CHARS,
                description='Target A1 notation range',
            ),
        ],
        values: Annotated[
            list[list[object]],
            Field(
                description='2D array of values to write',
            ),
        ],
        input_mode: Annotated[
            SheetsInputMode,
            Field(
                description='Value input mode (raw, user_entered)',
            ),
        ],
    ) -> SheetsWriteResult:
        """Write values to range."""
        return await run_gateway(
            gateway.update_range,
            spreadsheet_id,
            range_name,
            values,
            input_mode=input_mode,
        )

    @registrar.tool(
        name='sheets_append_rows',
        title='Append Rows to Spreadsheet',
        description=(
            'Append rows of data to a spreadsheet table following '
            'existing data.'
        ),
        annotations=append_annotations,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_append_rows(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
        range_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_A1_CHARS,
                description='Target A1 notation range identifying the table',
            ),
        ],
        values: Annotated[
            list[list[object]],
            Field(
                description='2D array of rows to append',
            ),
        ],
        input_mode: Annotated[
            SheetsInputMode,
            Field(
                description='Value input mode (raw, user_entered)',
            ),
        ],
        insert_mode: Annotated[
            SheetsInsertMode,
            Field(
                description='Row insert mode (insert_rows, overwrite)',
            ),
        ],
    ) -> SheetsAppendResult:
        """Append rows to table."""
        return await run_gateway(
            gateway.append_rows,
            spreadsheet_id,
            range_name,
            values,
            input_mode=input_mode,
            insert_mode=insert_mode,
        )

    @registrar.tool(
        name='sheets_batch_update_ranges',
        title='Batch Update Spreadsheet Ranges',
        description=(
            'Write values to multiple A1 notation ranges in one atomic call.'
        ),
        annotations=update_annotations,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_batch_update_ranges(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
        data: Annotated[
            list[SheetsWriteRange],
            Field(
                min_length=1,
                max_length=MAX_SHEETS_RANGES,
                description='List of range write specifications',
            ),
        ],
        input_mode: Annotated[
            SheetsInputMode,
            Field(
                description='Value input mode (raw, user_entered)',
            ),
        ],
    ) -> SheetsBatchWriteResult:
        """Batch write range values."""
        return await run_gateway(
            gateway.batch_update_ranges,
            spreadsheet_id,
            data,
            input_mode=input_mode,
        )

    @registrar.tool(
        name='sheets_clear_ranges',
        title='Clear Spreadsheet Ranges',
        description=(
            'Clear values and formulas from one or more A1 notation ranges.'
        ),
        annotations=clear_annotations,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_clear_ranges(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
        ranges: Annotated[
            list[
                Annotated[
                    str,
                    Field(min_length=1, max_length=MAX_SHEETS_A1_CHARS),
                ]
            ],
            Field(
                min_length=1,
                max_length=MAX_SHEETS_RANGES,
                description='List of A1 notation ranges to clear',
            ),
        ],
    ) -> SheetsClearResult:
        """Clear specified range values."""
        return await run_gateway(gateway.clear_ranges, spreadsheet_id, ranges)
