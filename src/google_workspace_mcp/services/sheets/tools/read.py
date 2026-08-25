"""Register Sheets read tools."""

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
    SheetsBatchReadResult,
    SheetsDateTimeMode,
    SheetsRenderMode,
    SheetsValueRange,
    SpreadsheetSummary,
)
from .common import run_gateway


def register_read_tools(
    registrar: ToolRegistrar,
    gateway: SheetsGateway,
) -> None:
    """Register Sheets read tools."""
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='sheets_get_spreadsheet',
        title='Get Spreadsheet Metadata',
        description=(
            'Get metadata and sheet properties for a Google Spreadsheet.'
        ),
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def sheets_get_spreadsheet(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
    ) -> SpreadsheetSummary:
        """Get spreadsheet metadata."""
        return await run_gateway(gateway.get_spreadsheet, spreadsheet_id)

    @registrar.tool(
        name='sheets_read_range',
        title='Read Spreadsheet Range',
        description=(
            'Read data from a single A1 notation range in a Google '
            'Spreadsheet.'
        ),
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def sheets_read_range(
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
                description='A1 notation range to read (e.g. Sheet1!A1:B10)',
            ),
        ],
        render_mode: Annotated[
            SheetsRenderMode,
            Field(
                description=(
                    'Value render mode (formatted, unformatted, formula)'
                ),
            ),
        ],
        date_time_mode: Annotated[
            SheetsDateTimeMode | None,
            Field(
                description=(
                    'Date/time render mode (serial_number, formatted_string)'
                ),
            ),
        ] = None,
    ) -> SheetsValueRange:
        """Read single range values."""
        return await run_gateway(
            gateway.read_range,
            spreadsheet_id,
            range_name,
            render_mode=render_mode,
            date_time_mode=date_time_mode,
        )

    @registrar.tool(
        name='sheets_batch_read_ranges',
        title='Batch Read Spreadsheet Ranges',
        description=(
            'Read data from multiple A1 notation ranges in a Google '
            'Spreadsheet.'
        ),
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def sheets_batch_read_ranges(
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
                description='List of A1 notation ranges to read',
            ),
        ],
        render_mode: Annotated[
            SheetsRenderMode,
            Field(
                description=(
                    'Value render mode (formatted, unformatted, formula)'
                ),
            ),
        ],
        date_time_mode: Annotated[
            SheetsDateTimeMode | None,
            Field(
                description=(
                    'Date/time render mode (serial_number, formatted_string)'
                ),
            ),
        ] = None,
    ) -> SheetsBatchReadResult:
        """Read multiple range values."""
        return await run_gateway(
            gateway.batch_read_ranges,
            spreadsheet_id,
            ranges,
            render_mode=render_mode,
            date_time_mode=date_time_mode,
        )
