"""Register Drive read tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DriveGateway
from ..constants import (
    MAX_DRIVE_ID_CHARS,
    MAX_DRIVE_MIME_TYPES,
    MAX_DRIVE_NAME_CHARS,
    MAX_DRIVE_PAGE_SIZE,
    MAX_DRIVE_TEXT_CHARS,
    MAX_DRIVE_TOKEN_CHARS,
)
from ..schemas import DriveFile, DriveFileList, DriveSearchFilters
from .common import run_gateway


def register_drive_read_tools(
    registrar: ToolRegistrar,
    gateway: DriveGateway,
) -> None:
    """Register Drive read tools."""
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='drive_search_files',
        title='Search Drive Files',
        description='Search Drive files matching structured filters.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def drive_search_files(
        text: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_TEXT_CHARS,
                description='Text for name and content matching',
            ),
        ] = None,
        exact_name: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_NAME_CHARS,
                description='Exact filename to match',
            ),
        ] = None,
        parent_id: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Parent folder identifier',
            ),
        ] = None,
        mime_types: Annotated[
            list[
                Annotated[
                    str,
                    Field(
                        min_length=1,
                        max_length=255,
                        description='MIME type filter',
                    ),
                ]
            ],
            Field(
                max_length=MAX_DRIVE_MIME_TYPES,
                description='List of MIME types to match',
            ),
        ] = [],
        modified_after: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=128,
                description='Strict RFC3339 lower timestamp bound',
            ),
        ] = None,
        modified_before: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=128,
                description='Strict RFC3339 upper timestamp bound',
            ),
        ] = None,
        drive_id: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Shared drive identifier',
            ),
        ] = None,
        page_size: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_DRIVE_PAGE_SIZE,
                description='Maximum files to return',
            ),
        ] = MAX_DRIVE_PAGE_SIZE,
        page_token: Annotated[
            str | None,
            Field(
                default=None,
                max_length=MAX_DRIVE_TOKEN_CHARS,
                description='Opaque pagination page token',
            ),
        ] = None,
    ) -> DriveFileList:
        """Search bounded Drive files."""
        filters = DriveSearchFilters(
            text=text,
            exact_name=exact_name,
            parent_id=parent_id,
            mime_types=tuple(mime_types),
            modified_after=modified_after,
            modified_before=modified_before,
            drive_id=drive_id,
        )
        return await run_gateway(
            gateway.search_files,
            filters,
            page_size,
            page_token,
        )

    @registrar.tool(
        name='drive_get_file',
        title='Get Drive File',
        description='Retrieve metadata for a single Drive file.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def drive_get_file(
        file_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Target Drive file identifier',
            ),
        ],
    ) -> DriveFile:
        """Read one Drive file."""
        return await run_gateway(gateway.get_file, file_id)

    @registrar.tool(
        name='drive_list_folder',
        title='List Folder Contents',
        description='List files inside a Drive folder.',
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def drive_list_folder(
        folder_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Parent Drive folder identifier',
            ),
        ],
        page_size: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_DRIVE_PAGE_SIZE,
                description='Maximum files to return',
            ),
        ] = MAX_DRIVE_PAGE_SIZE,
        page_token: Annotated[
            str | None,
            Field(
                default=None,
                max_length=MAX_DRIVE_TOKEN_CHARS,
                description='Opaque pagination page token',
            ),
        ] = None,
        drive_id: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Shared drive identifier',
            ),
        ] = None,
    ) -> DriveFileList:
        """List Drive folder contents."""
        return await run_gateway(
            gateway.list_folder,
            folder_id,
            page_size,
            page_token,
            drive_id,
        )
