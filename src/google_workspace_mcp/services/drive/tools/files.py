"""Register Drive file tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.common.managed_files import ManagedFileStore
from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DriveGateway
from ..constants import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_ID_CHARS,
    MAX_DRIVE_NAME_CHARS,
)
from ..schemas import (
    DriveExportFormat,
    DriveManagedFile,
    DriveMutationResult,
)
from .common import run_gateway


def register_file_tools(
    registrar: ToolRegistrar,
    gateway: DriveGateway,
    files: ManagedFileStore,
) -> None:
    """Register Drive file tools."""
    non_destructive = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
    destructive = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @registrar.tool(
        name='drive_download_file',
        title='Download Drive File',
        description='Download binary Drive file into local managed storage.',
        annotations=non_destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_download_file(
        file_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Target Drive file identifier',
            ),
        ],
    ) -> DriveManagedFile:
        """Download binary Drive file."""
        return await run_gateway(gateway.download_file, file_id, files)

    @registrar.tool(
        name='drive_export_file',
        title='Export Drive File',
        description=(
            'Export Google Workspace document into local managed storage. '
            'CSV format exports only the first sheet per provider behavior.'
        ),
        annotations=non_destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_export_file(
        file_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Target Google Workspace file identifier',
            ),
        ],
        export_format: Annotated[
            DriveExportFormat,
            Field(
                description='Target export format',
            ),
        ],
    ) -> DriveManagedFile:
        """Export Workspace document file."""
        return await run_gateway(
            gateway.export_file,
            file_id,
            export_format,
            files,
        )

    @registrar.tool(
        name='drive_create_folder',
        title='Create Drive Folder',
        description='Create a new folder in Google Drive.',
        annotations=non_destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_create_folder(
        name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_NAME_CHARS,
                description='Folder name',
            ),
        ],
        parent_id: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Parent folder identifier',
            ),
        ] = None,
    ) -> DriveMutationResult:
        """Create Drive parent folder."""
        return await run_gateway(gateway.create_folder, name, parent_id)

    @registrar.tool(
        name='drive_upload_file',
        title='Upload Drive File',
        description='Upload a managed local file into Google Drive.',
        annotations=non_destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_upload_file(
        managed_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=255,
                description='Managed local file basename',
            ),
        ],
        expected_size: Annotated[
            int,
            Field(
                ge=0,
                le=MAX_DRIVE_DOWNLOAD_BYTES,
                description='Expected file size in bytes',
            ),
        ],
        expected_sha256: Annotated[
            str,
            Field(
                min_length=64,
                max_length=64,
                description='Expected SHA-256 digest in hex',
            ),
        ],
        name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_NAME_CHARS,
                description='Target Drive file name',
            ),
        ],
        mime_type: Annotated[
            str,
            Field(
                min_length=1,
                max_length=255,
                description='Target MIME type',
            ),
        ],
        parent_id: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Parent folder identifier',
            ),
        ] = None,
    ) -> DriveMutationResult:
        """Upload managed local file."""
        return await run_gateway(
            gateway.upload_file,
            managed_name,
            expected_size,
            expected_sha256,
            name,
            mime_type,
            parent_id,
            files,
        )

    @registrar.tool(
        name='drive_update_file',
        title='Update Drive File',
        description=(
            'Update file metadata and/or content with best-effort version '
            'preflight check (non-atomic last-write-wins). Requires at least '
            'one metadata or content change.'
        ),
        annotations=destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_update_file(
        file_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Target Drive file identifier',
            ),
        ],
        expected_version: Annotated[
            int,
            Field(
                ge=0,
                description='Expected file version for best-effort preflight',
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_NAME_CHARS,
                description='New Drive file name',
            ),
        ] = None,
        managed_name: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=255,
                description=(
                    'Managed local file basename for content replacement'
                ),
            ),
        ] = None,
        expected_size: Annotated[
            int | None,
            Field(
                default=None,
                ge=0,
                le=MAX_DRIVE_DOWNLOAD_BYTES,
                description='Expected replacement file size in bytes',
            ),
        ] = None,
        expected_sha256: Annotated[
            str | None,
            Field(
                default=None,
                min_length=64,
                max_length=64,
                description='Expected replacement SHA-256 digest in hex',
            ),
        ] = None,
        mime_type: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=255,
                description='Replacement MIME type',
            ),
        ] = None,
    ) -> DriveMutationResult:
        """Update Drive file content."""
        return await run_gateway(
            gateway.update_file,
            file_id,
            expected_version,
            name,
            managed_name,
            expected_size,
            expected_sha256,
            mime_type,
            files,
        )

    @registrar.tool(
        name='drive_move_file',
        title='Move Drive File',
        description=(
            'Move file to new parent folder with best-effort version '
            'preflight check (non-atomic).'
        ),
        annotations=destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_move_file(
        file_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Target Drive file identifier',
            ),
        ],
        expected_version: Annotated[
            int,
            Field(
                ge=0,
                description='Expected file version for best-effort preflight',
            ),
        ],
        destination_parent_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Destination parent folder identifier',
            ),
        ],
    ) -> DriveMutationResult:
        """Move Drive file parent."""
        return await run_gateway(
            gateway.move_file,
            file_id,
            expected_version,
            destination_parent_id,
        )

    @registrar.tool(
        name='drive_copy_file',
        title='Copy Drive File',
        description='Create app-owned copy of a Drive file.',
        annotations=non_destructive,
        structured_output=True,
        available_to_readonly=False,
    )
    async def drive_copy_file(
        file_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Source Drive file identifier',
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_NAME_CHARS,
                description='New name for copied file',
            ),
        ] = None,
        parent_id: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_DRIVE_ID_CHARS,
                description='Destination parent folder identifier',
            ),
        ] = None,
    ) -> DriveMutationResult:
        """Copy Drive source file."""
        return await run_gateway(
            gateway.copy_file,
            file_id,
            name,
            parent_id,
        )
