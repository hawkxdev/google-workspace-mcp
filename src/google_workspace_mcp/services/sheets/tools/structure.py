"""Register Sheets structure tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import SheetsGateway
from ..constants import (
    MAX_SHEETS_GRID_CELLS,
    MAX_SHEETS_TEXT_CHARS,
    MAX_SHEETS_TITLE_CHARS,
)
from ..schemas import (
    SheetCopyResult,
    SheetMutationResult,
    SpreadsheetCreateResult,
)
from .common import run_gateway


def register_structure_tools(
    registrar: ToolRegistrar,
    gateway: SheetsGateway,
) -> None:
    """Register Sheets structure tools."""
    non_destructive_non_idempotent = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
    rename_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='sheets_create_spreadsheet',
        title='Create Spreadsheet',
        description='Create a new blank Google Spreadsheet.',
        annotations=non_destructive_non_idempotent,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_create_spreadsheet(
        title: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TITLE_CHARS,
                description='Title for the new spreadsheet',
            ),
        ],
        locale: Annotated[
            str | None,
            Field(
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet locale (e.g. en_US, ru_RU)',
            ),
        ] = None,
        time_zone: Annotated[
            str | None,
            Field(
                max_length=MAX_SHEETS_TEXT_CHARS,
                description=(
                    'Spreadsheet time zone (e.g. America/New_York, '
                    'Europe/Moscow)'
                ),
            ),
        ] = None,
    ) -> SpreadsheetCreateResult:
        """Create new Google spreadsheet."""
        return await run_gateway(
            gateway.create_spreadsheet,
            title,
            locale=locale,
            time_zone=time_zone,
        )

    @registrar.tool(
        name='sheets_add_sheet',
        title='Add Sheet to Spreadsheet',
        description='Add a new sheet tab to an existing Google Spreadsheet.',
        annotations=non_destructive_non_idempotent,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_add_sheet(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
        title: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TITLE_CHARS,
                description='Title of the new sheet',
            ),
        ],
        row_count: Annotated[
            int | None,
            Field(
                ge=1,
                le=MAX_SHEETS_GRID_CELLS,
                description='Initial row count for grid',
            ),
        ] = None,
        column_count: Annotated[
            int | None,
            Field(
                ge=1,
                le=MAX_SHEETS_GRID_CELLS,
                description='Initial column count for grid',
            ),
        ] = None,
    ) -> SheetMutationResult:
        """Add sheet to spreadsheet."""
        return await run_gateway(
            gateway.add_sheet,
            spreadsheet_id,
            title,
            row_count=row_count,
            column_count=column_count,
        )

    @registrar.tool(
        name='sheets_rename_sheet',
        title='Rename Sheet in Spreadsheet',
        description=('Rename an existing sheet tab in a Google Spreadsheet.'),
        annotations=rename_annotations,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_rename_sheet(
        spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Spreadsheet identifier',
            ),
        ],
        sheet_id: Annotated[
            int,
            Field(
                ge=0,
                description='Numeric ID of the sheet to rename',
            ),
        ],
        title: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TITLE_CHARS,
                description='New sheet title',
            ),
        ],
    ) -> SheetMutationResult:
        """Rename existing sheet tab."""
        return await run_gateway(
            gateway.rename_sheet,
            spreadsheet_id,
            sheet_id,
            title,
        )

    @registrar.tool(
        name='sheets_copy_sheet',
        title='Copy Sheet',
        description=(
            'Copy a sheet within the same spreadsheet or to another '
            'spreadsheet.'
        ),
        annotations=non_destructive_non_idempotent,
        structured_output=True,
        available_to_readonly=False,
    )
    async def sheets_copy_sheet(
        source_spreadsheet_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description='Source spreadsheet identifier',
            ),
        ],
        sheet_id: Annotated[
            int,
            Field(
                ge=0,
                description='Numeric ID of the sheet to copy',
            ),
        ],
        destination_spreadsheet_id: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TEXT_CHARS,
                description=(
                    'Destination spreadsheet ID (omit for duplicate in same '
                    'spreadsheet)'
                ),
            ),
        ] = None,
        title: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=MAX_SHEETS_TITLE_CHARS,
                description='Title for duplicate in same spreadsheet',
            ),
        ] = None,
    ) -> SheetCopyResult:
        """Copy spreadsheet sheet tab."""
        target_dest_id = destination_spreadsheet_id or source_spreadsheet_id
        return await run_gateway(
            gateway.copy_sheet,
            source_spreadsheet_id,
            sheet_id,
            target_dest_id,
            new_title=title,
        )
