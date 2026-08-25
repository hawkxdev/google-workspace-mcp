"""Test Drive MCP tools."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.auth import context
from google_workspace_mcp.services.drive.constants import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_ID_CHARS,
    MAX_DRIVE_MIME_TYPES,
    MAX_DRIVE_NAME_CHARS,
    MAX_DRIVE_PAGE_SIZE,
    MAX_DRIVE_TEXT_CHARS,
    MAX_DRIVE_TOKEN_CHARS,
)
from google_workspace_mcp.services.drive.errors import (
    DriveConflictError,
    DriveInputError,
    DriveProviderError,
    DriveScopeError,
)
from google_workspace_mcp.services.drive.schemas import (
    DriveExportFormat,
    DriveFile,
    DriveFileList,
    DriveManagedFile,
    DriveMutationResult,
    DriveSearchFilters,
)
from google_workspace_mcp.services.drive.tools import (
    register_drive_tools,
)
from google_workspace_mcp.services.drive.tools.common import run_gateway
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)

TOOL_NAMES = {
    'drive_search_files',
    'drive_get_file',
    'drive_list_folder',
    'drive_download_file',
    'drive_export_file',
    'drive_create_folder',
    'drive_upload_file',
    'drive_update_file',
    'drive_move_file',
    'drive_copy_file',
}

READONLY_TOOL_NAMES = {
    'drive_search_files',
    'drive_get_file',
    'drive_list_folder',
}

FULL_ONLY_NON_DESTRUCTIVE_TOOLS = {
    'drive_download_file',
    'drive_export_file',
    'drive_create_folder',
    'drive_upload_file',
    'drive_copy_file',
}

FULL_ONLY_DESTRUCTIVE_TOOLS = {
    'drive_update_file',
    'drive_move_file',
}


class UnusedGateway:
    def __getattr__(self, name: str) -> Any:
        def operation(*_: object, **__: object) -> object:
            raise AssertionError(f'unexpected gateway call: {name}')

        return operation


class UnusedFileStore:
    def __getattr__(self, name: str) -> Any:
        def operation(*_: object, **__: object) -> object:
            raise AssertionError(f'unexpected file store call: {name}')

        return operation


def _prop_schema(prop: dict[str, Any]) -> dict[str, Any]:
    if 'anyOf' in prop:
        return next(sub for sub in prop['anyOf'] if sub.get('type') != 'null')
    return prop


@pytest.mark.asyncio
async def test_registers_exact_drive_inventory_flat_schemas() -> None:
    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        UnusedGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='full_principal',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)

    assert {tool.name for tool in tools} == TOOL_NAMES
    assert set(server.readonly_capabilities()) == READONLY_TOOL_NAMES

    public = {tool.name: tool for tool in tools}

    # Prohibited tool kinds
    prohibited_substrings = (
        'delete',
        'trash',
        'share',
        'permission',
        'comment',
        'revision',
        'restore',
    )
    for name in TOOL_NAMES:
        for prohibited in prohibited_substrings:
            assert prohibited not in name

    # Search
    search = public['drive_search_files']
    assert 'q' not in search.input_schema['properties']
    assert 'params' not in search.input_schema['properties']
    assert set(search.input_schema['properties']) == {
        'text',
        'exact_name',
        'parent_id',
        'mime_types',
        'modified_after',
        'modified_before',
        'drive_id',
        'page_size',
        'page_token',
    }
    assert search.output_schema is not None
    search_props = search.input_schema['properties']
    assert (
        _prop_schema(search_props['text'])['maxLength'] == MAX_DRIVE_TEXT_CHARS
    )
    assert _prop_schema(search_props['text'])['minLength'] == 1
    assert (
        _prop_schema(search_props['exact_name'])['maxLength']
        == MAX_DRIVE_NAME_CHARS
    )
    assert _prop_schema(search_props['exact_name'])['minLength'] == 1
    assert (
        _prop_schema(search_props['parent_id'])['maxLength']
        == MAX_DRIVE_ID_CHARS
    )
    assert _prop_schema(search_props['parent_id'])['minLength'] == 1
    assert search_props['mime_types']['maxItems'] == MAX_DRIVE_MIME_TYPES
    assert search_props['mime_types']['items']['maxLength'] == 255
    assert search_props['mime_types']['items']['minLength'] == 1
    assert _prop_schema(search_props['modified_after'])['maxLength'] == 128
    assert _prop_schema(search_props['modified_after'])['minLength'] == 1
    assert _prop_schema(search_props['modified_before'])['maxLength'] == 128
    assert _prop_schema(search_props['modified_before'])['minLength'] == 1
    assert (
        _prop_schema(search_props['drive_id'])['maxLength']
        == MAX_DRIVE_ID_CHARS
    )
    assert _prop_schema(search_props['drive_id'])['minLength'] == 1
    assert search_props['page_size']['minimum'] == 1
    assert search_props['page_size']['maximum'] == MAX_DRIVE_PAGE_SIZE
    assert (
        _prop_schema(search_props['page_token'])['maxLength']
        == MAX_DRIVE_TOKEN_CHARS
    )

    # Get file
    get_file = public['drive_get_file']
    assert 'params' not in get_file.input_schema['properties']
    assert set(get_file.input_schema['properties']) == {'file_id'}
    assert get_file.output_schema is not None
    get_file_props = get_file.input_schema['properties']
    assert get_file_props['file_id']['minLength'] == 1
    assert get_file_props['file_id']['maxLength'] == MAX_DRIVE_ID_CHARS

    # List folder
    list_folder = public['drive_list_folder']
    assert 'q' not in list_folder.input_schema['properties']
    assert 'params' not in list_folder.input_schema['properties']
    assert set(list_folder.input_schema['properties']) == {
        'folder_id',
        'page_size',
        'page_token',
        'drive_id',
    }
    assert list_folder.output_schema is not None
    list_folder_props = list_folder.input_schema['properties']
    assert list_folder_props['folder_id']['minLength'] == 1
    assert list_folder_props['folder_id']['maxLength'] == MAX_DRIVE_ID_CHARS
    assert list_folder_props['page_size']['minimum'] == 1
    assert list_folder_props['page_size']['maximum'] == MAX_DRIVE_PAGE_SIZE
    assert (
        _prop_schema(list_folder_props['page_token'])['maxLength']
        == MAX_DRIVE_TOKEN_CHARS
    )
    assert (
        _prop_schema(list_folder_props['drive_id'])['maxLength']
        == MAX_DRIVE_ID_CHARS
    )
    assert _prop_schema(list_folder_props['drive_id'])['minLength'] == 1

    # Download file
    download_file = public['drive_download_file']
    assert 'params' not in download_file.input_schema['properties']
    assert 'acknowledgeAbuse' not in download_file.input_schema['properties']
    assert 'acknowledge_abuse' not in download_file.input_schema['properties']
    assert set(download_file.input_schema['properties']) == {'file_id'}
    assert download_file.output_schema is not None
    assert 'managed' in (download_file.description or '').lower()
    download_props = download_file.input_schema['properties']
    assert download_props['file_id']['minLength'] == 1
    assert download_props['file_id']['maxLength'] == MAX_DRIVE_ID_CHARS

    # Export file
    export_file = public['drive_export_file']
    assert 'params' not in export_file.input_schema['properties']
    assert 'mime_type' not in export_file.input_schema['properties']
    assert 'mimeType' not in export_file.input_schema['properties']
    assert set(export_file.input_schema['properties']) == {
        'file_id',
        'export_format',
    }
    assert export_file.output_schema is not None
    export_desc = export_file.description or ''
    assert 'managed' in export_desc.lower()
    assert 'csv' in export_desc.lower()
    assert 'first sheet' in export_desc.lower()
    export_props = export_file.input_schema['properties']
    assert export_props['file_id']['minLength'] == 1
    assert export_props['file_id']['maxLength'] == MAX_DRIVE_ID_CHARS
    if '$ref' in export_props['export_format']:
        ref_ref = export_props['export_format']['$ref']
        ref_key = ref_ref.removeprefix('#/$defs/')
        assert export_file.input_schema['$defs'][ref_key]['enum'] == [
            fmt.value for fmt in DriveExportFormat
        ]
    else:
        assert _prop_schema(export_props['export_format'])['enum'] == [
            fmt.value for fmt in DriveExportFormat
        ]

    # Create folder
    create_folder = public['drive_create_folder']
    assert 'params' not in create_folder.input_schema['properties']
    assert 'mime_type' not in create_folder.input_schema['properties']
    assert 'mimeType' not in create_folder.input_schema['properties']
    assert set(create_folder.input_schema['properties']) == {
        'name',
        'parent_id',
    }
    assert create_folder.output_schema is not None
    create_folder_props = create_folder.input_schema['properties']
    assert create_folder_props['name']['minLength'] == 1
    assert create_folder_props['name']['maxLength'] == MAX_DRIVE_NAME_CHARS
    assert _prop_schema(create_folder_props['parent_id'])['minLength'] == 1
    assert (
        _prop_schema(create_folder_props['parent_id'])['maxLength']
        == MAX_DRIVE_ID_CHARS
    )

    # Upload file
    upload_file = public['drive_upload_file']
    assert 'params' not in upload_file.input_schema['properties']
    assert 'path' not in upload_file.input_schema['properties']
    assert 'file_path' not in upload_file.input_schema['properties']
    assert 'local_path' not in upload_file.input_schema['properties']
    assert set(upload_file.input_schema['properties']) == {
        'managed_name',
        'expected_size',
        'expected_sha256',
        'name',
        'mime_type',
        'parent_id',
    }
    assert upload_file.output_schema is not None
    upload_props = upload_file.input_schema['properties']
    assert upload_props['managed_name']['minLength'] == 1
    assert upload_props['managed_name']['maxLength'] == 255
    assert upload_props['expected_size']['minimum'] == 0
    assert upload_props['expected_size']['maximum'] == MAX_DRIVE_DOWNLOAD_BYTES
    assert upload_props['expected_sha256']['minLength'] == 64
    assert upload_props['expected_sha256']['maxLength'] == 64
    assert upload_props['name']['minLength'] == 1
    assert upload_props['name']['maxLength'] == MAX_DRIVE_NAME_CHARS
    assert upload_props['mime_type']['minLength'] == 1
    assert upload_props['mime_type']['maxLength'] == 255
    assert _prop_schema(upload_props['parent_id'])['minLength'] == 1
    assert (
        _prop_schema(upload_props['parent_id'])['maxLength']
        == MAX_DRIVE_ID_CHARS
    )

    # Update file
    update_file = public['drive_update_file']
    assert 'params' not in update_file.input_schema['properties']
    assert 'path' not in update_file.input_schema['properties']
    assert 'file_path' not in update_file.input_schema['properties']
    assert 'local_path' not in update_file.input_schema['properties']
    assert set(update_file.input_schema['properties']) == {
        'file_id',
        'expected_version',
        'name',
        'managed_name',
        'expected_size',
        'expected_sha256',
        'mime_type',
    }
    assert update_file.output_schema is not None
    update_desc = update_file.description or ''
    assert 'best-effort' in update_desc.lower()
    assert 'version preflight' in update_desc.lower()
    update_props = update_file.input_schema['properties']
    assert update_props['file_id']['minLength'] == 1
    assert update_props['file_id']['maxLength'] == MAX_DRIVE_ID_CHARS
    assert update_props['expected_version']['minimum'] == 0
    assert _prop_schema(update_props['name'])['minLength'] == 1
    assert (
        _prop_schema(update_props['name'])['maxLength'] == MAX_DRIVE_NAME_CHARS
    )
    assert _prop_schema(update_props['managed_name'])['minLength'] == 1
    assert _prop_schema(update_props['managed_name'])['maxLength'] == 255
    assert _prop_schema(update_props['expected_size'])['minimum'] == 0
    assert (
        _prop_schema(update_props['expected_size'])['maximum']
        == MAX_DRIVE_DOWNLOAD_BYTES
    )
    assert _prop_schema(update_props['expected_sha256'])['minLength'] == 64
    assert _prop_schema(update_props['expected_sha256'])['maxLength'] == 64
    assert _prop_schema(update_props['mime_type'])['minLength'] == 1
    assert _prop_schema(update_props['mime_type'])['maxLength'] == 255

    # Move file
    move_file = public['drive_move_file']
    assert 'params' not in move_file.input_schema['properties']
    assert set(move_file.input_schema['properties']) == {
        'file_id',
        'expected_version',
        'destination_parent_id',
    }
    assert move_file.output_schema is not None
    move_desc = move_file.description or ''
    assert 'best-effort' in move_desc.lower()
    assert 'version preflight' in move_desc.lower()
    move_props = move_file.input_schema['properties']
    assert move_props['file_id']['minLength'] == 1
    assert move_props['file_id']['maxLength'] == MAX_DRIVE_ID_CHARS
    assert move_props['expected_version']['minimum'] == 0
    assert move_props['destination_parent_id']['minLength'] == 1
    assert (
        move_props['destination_parent_id']['maxLength'] == MAX_DRIVE_ID_CHARS
    )

    # Copy file
    copy_file = public['drive_copy_file']
    assert 'params' not in copy_file.input_schema['properties']
    assert set(copy_file.input_schema['properties']) == {
        'file_id',
        'name',
        'parent_id',
    }
    assert copy_file.output_schema is not None
    copy_props = copy_file.input_schema['properties']
    assert copy_props['file_id']['minLength'] == 1
    assert copy_props['file_id']['maxLength'] == MAX_DRIVE_ID_CHARS
    assert _prop_schema(copy_props['name'])['minLength'] == 1
    assert (
        _prop_schema(copy_props['name'])['maxLength'] == MAX_DRIVE_NAME_CHARS
    )
    assert _prop_schema(copy_props['parent_id'])['minLength'] == 1
    assert (
        _prop_schema(copy_props['parent_id'])['maxLength']
        == MAX_DRIVE_ID_CHARS
    )


def test_drive_annotations_match_side_effects() -> None:
    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        UnusedGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )
    tools = server._tool_manager.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert {tool.name for tool in tools} == TOOL_NAMES

    for name in READONLY_TOOL_NAMES:
        annotations = by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is True
        assert annotations.open_world_hint is True

    for name in FULL_ONLY_NON_DESTRUCTIVE_TOOLS:
        annotations = by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is False
        assert annotations.open_world_hint is True

    for name in FULL_ONLY_DESTRUCTIVE_TOOLS:
        annotations = by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is True
        assert annotations.idempotent_hint is False
        assert annotations.open_world_hint is True


@pytest.mark.asyncio
async def test_search_files_delegates_flat_params_to_filters_and_gateway() -> (
    None
):
    recorded_calls: list[tuple[DriveSearchFilters, int, str | None]] = []

    class SearchGateway(UnusedGateway):
        def search_files(
            self,
            filters: DriveSearchFilters,
            page_size: int = MAX_DRIVE_PAGE_SIZE,
            page_token: str | None = None,
        ) -> DriveFileList:
            recorded_calls.append((filters, page_size, page_token))
            return DriveFileList(
                files=(
                    DriveFile(
                        file_id='file_1',
                        name='plan.docx',
                        mime_type=(
                            'application/vnd.openxmlformats-officedocument.'
                            'wordprocessingml.document'
                        ),
                        size=1024,
                    ),
                ),
                next_page_token='next_token_123',
                incomplete_search=False,
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        SearchGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_search_files',
            {
                'text': 'quarterly report',
                'exact_name': 'plan.docx',
                'parent_id': 'folder_99',
                'mime_types': [
                    'application/vnd.openxmlformats-officedocument.'
                    'wordprocessingml.document'
                ],
                'modified_after': '2026-01-01T00:00:00Z',
                'modified_before': '2026-02-01T00:00:00Z',
                'drive_id': 'drive_team_1',
                'page_size': 15,
                'page_token': 'token_start',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    filters, page_size, page_token = recorded_calls[0]
    assert filters.text == 'quarterly report'
    assert filters.exact_name == 'plan.docx'
    assert filters.parent_id == 'folder_99'
    assert filters.mime_types == (
        'application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document',
    )
    assert filters.modified_after == '2026-01-01T00:00:00Z'
    assert filters.modified_before == '2026-02-01T00:00:00Z'
    assert filters.drive_id == 'drive_team_1'
    assert page_size == 15
    assert page_token == 'token_start'

    assert result.structured_content['files'][0]['file_id'] == 'file_1'
    assert result.structured_content['files'][0]['name'] == 'plan.docx'
    assert result.structured_content['next_page_token'] == 'next_token_123'


@pytest.mark.asyncio
async def test_readonly_principal_capability_filtering() -> None:
    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        UnusedGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='readonly_user',
        credential_id='0' * 64,
        client_id='client',
        policy='custom_readonly',
        capabilities=frozenset({'drive_get_file'}),
        full_access=False,
    )
    token = context.set_request_context(principal, 'request')
    try:
        tools = await server.list_tools()
    finally:
        context.reset_request_context(token)

    assert {tool.name for tool in tools} == {'drive_get_file'}


@pytest.mark.asyncio
async def test_search_files_delegates_default_parameters() -> None:
    recorded_calls: list[tuple[DriveSearchFilters, int, str | None]] = []

    class SearchGateway(UnusedGateway):
        def search_files(
            self,
            filters: DriveSearchFilters,
            page_size: int = MAX_DRIVE_PAGE_SIZE,
            page_token: str | None = None,
        ) -> DriveFileList:
            recorded_calls.append((filters, page_size, page_token))
            return DriveFileList(
                files=(),
                next_page_token='',
                incomplete_search=False,
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        SearchGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool('drive_search_files', {})
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    filters, page_size, page_token = recorded_calls[0]
    assert filters.text is None
    assert filters.exact_name is None
    assert filters.parent_id is None
    assert filters.mime_types == ()
    assert filters.modified_after is None
    assert filters.modified_before is None
    assert filters.drive_id is None
    assert page_size == MAX_DRIVE_PAGE_SIZE
    assert page_token is None
    assert result.structured_content['files'] == []


@pytest.mark.asyncio
async def test_get_file_delegates_to_gateway() -> None:
    recorded_calls: list[str] = []

    class GetFileGateway(UnusedGateway):
        def get_file(self, file_id: str) -> DriveFile:
            recorded_calls.append(file_id)
            return DriveFile(
                file_id=file_id,
                name='notes.txt',
                mime_type='text/plain',
                size=42,
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        GetFileGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_get_file',
            {'file_id': 'file_target_123'},
        )
    finally:
        context.reset_request_context(token)

    assert recorded_calls == ['file_target_123']
    assert result.structured_content['file_id'] == 'file_target_123'
    assert result.structured_content['name'] == 'notes.txt'
    assert result.structured_content['size'] == 42


@pytest.mark.asyncio
async def test_list_folder_delegates_to_gateway() -> None:
    recorded_calls: list[tuple[str, int, str | None, str | None]] = []

    class ListFolderGateway(UnusedGateway):
        def list_folder(
            self,
            folder_id: str,
            page_size: int = MAX_DRIVE_PAGE_SIZE,
            page_token: str | None = None,
            drive_id: str | None = None,
        ) -> DriveFileList:
            recorded_calls.append((folder_id, page_size, page_token, drive_id))
            return DriveFileList(
                files=(
                    DriveFile(
                        file_id='item_1',
                        name='subfolder',
                        mime_type='application/vnd.google-apps.folder',
                    ),
                ),
                next_page_token='tok_folder_next',
                incomplete_search=False,
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        ListFolderGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_list_folder',
            {
                'folder_id': 'folder_root_1',
                'page_size': 20,
                'page_token': 'tok_folder_prev',
                'drive_id': 'shared_drive_88',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == (
        'folder_root_1',
        20,
        'tok_folder_prev',
        'shared_drive_88',
    )
    assert result.structured_content['files'][0]['file_id'] == 'item_1'
    assert result.structured_content['next_page_token'] == 'tok_folder_next'


@pytest.mark.asyncio
async def test_download_file_delegates_to_gateway_and_files() -> None:
    recorded_calls: list[tuple[str, Any]] = []
    file_store = UnusedFileStore()

    class DownloadGateway(UnusedGateway):
        def download_file(
            self,
            file_id: str,
            files: Any,
        ) -> DriveManagedFile:
            recorded_calls.append((file_id, files))
            return DriveManagedFile(
                managed_name='managed_abc.pdf',
                original_name='report.pdf',
                mime_type='application/pdf',
                size=1234,
                sha256='b' * 64,
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        DownloadGateway(),  # type: ignore[arg-type]
        file_store,  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_download_file',
            {'file_id': 'file_download_1'},
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == ('file_download_1', file_store)
    assert result.structured_content['managed_name'] == 'managed_abc.pdf'
    assert result.structured_content['original_name'] == 'report.pdf'
    assert result.structured_content['size'] == 1234
    assert result.structured_content['sha256'] == 'b' * 64


@pytest.mark.asyncio
async def test_export_file_delegates_to_gateway_and_files() -> None:
    recorded_calls: list[tuple[str, DriveExportFormat | str, Any]] = []
    file_store = UnusedFileStore()

    class ExportGateway(UnusedGateway):
        def export_file(
            self,
            file_id: str,
            export_format: DriveExportFormat | str,
            files: Any,
        ) -> DriveManagedFile:
            recorded_calls.append((file_id, export_format, files))
            return DriveManagedFile(
                managed_name='managed_doc.docx',
                original_name='Doc.docx',
                mime_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                size=5678,
                sha256='c' * 64,
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        ExportGateway(),  # type: ignore[arg-type]
        file_store,  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_export_file',
            {
                'file_id': 'doc_export_1',
                'export_format': 'docx',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == (
        'doc_export_1',
        DriveExportFormat.DOCX,
        file_store,
    )
    assert result.structured_content['managed_name'] == 'managed_doc.docx'
    assert result.structured_content['original_name'] == 'Doc.docx'
    assert result.structured_content['size'] == 5678
    assert result.structured_content['sha256'] == 'c' * 64


@pytest.mark.asyncio
async def test_create_folder_delegates_to_gateway() -> None:
    recorded_calls: list[tuple[str, str | None]] = []

    class CreateFolderGateway(UnusedGateway):
        def create_folder(
            self,
            name: str,
            parent_id: str | None = None,
        ) -> DriveMutationResult:
            recorded_calls.append((name, parent_id))
            return DriveMutationResult(
                file=DriveFile(
                    file_id='new_folder_id',
                    name=name,
                    mime_type='application/vnd.google-apps.folder',
                    parents=(parent_id,) if parent_id else (),
                )
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        CreateFolderGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_create_folder',
            {
                'name': 'New Project',
                'parent_id': 'parent_root',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == ('New Project', 'parent_root')
    assert result.structured_content['file']['file_id'] == 'new_folder_id'
    assert result.structured_content['file']['name'] == 'New Project'


@pytest.mark.asyncio
async def test_upload_file_delegates_to_gateway_and_files() -> None:
    recorded_calls: list[tuple[str, int, str, str, str, str | None, Any]] = []
    file_store = UnusedFileStore()

    class UploadGateway(UnusedGateway):
        def upload_file(
            self,
            managed_name: str,
            expected_size: int,
            expected_sha256: str,
            name: str,
            mime_type: str,
            parent_id: str | None,
            files: Any,
        ) -> DriveMutationResult:
            recorded_calls.append(
                (
                    managed_name,
                    expected_size,
                    expected_sha256,
                    name,
                    mime_type,
                    parent_id,
                    files,
                )
            )
            return DriveMutationResult(
                file=DriveFile(
                    file_id='uploaded_file_id',
                    name=name,
                    mime_type=mime_type,
                    size=expected_size,
                    parents=(parent_id,) if parent_id else (),
                )
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        UploadGateway(),  # type: ignore[arg-type]
        file_store,  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_upload_file',
            {
                'managed_name': 'local_upload.txt',
                'expected_size': 128,
                'expected_sha256': 'd' * 64,
                'name': 'drive_target.txt',
                'mime_type': 'text/plain',
                'parent_id': 'folder_dest',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == (
        'local_upload.txt',
        128,
        'd' * 64,
        'drive_target.txt',
        'text/plain',
        'folder_dest',
        file_store,
    )
    assert result.structured_content['file']['file_id'] == 'uploaded_file_id'
    assert result.structured_content['file']['name'] == 'drive_target.txt'


@pytest.mark.asyncio
async def test_update_file_delegates_to_gateway_and_files() -> None:
    recorded_calls: list[
        tuple[
            str,
            int,
            str | None,
            str | None,
            int | None,
            str | None,
            str | None,
            Any,
        ]
    ] = []
    file_store = UnusedFileStore()

    class UpdateGateway(UnusedGateway):
        def update_file(
            self,
            file_id: str,
            expected_version: int,
            name: str | None,
            managed_name: str | None,
            expected_size: int | None,
            expected_sha256: str | None,
            mime_type: str | None,
            files: Any,
        ) -> DriveMutationResult:
            recorded_calls.append(
                (
                    file_id,
                    expected_version,
                    name,
                    managed_name,
                    expected_size,
                    expected_sha256,
                    mime_type,
                    files,
                )
            )
            return DriveMutationResult(
                file=DriveFile(
                    file_id=file_id,
                    name=name or 'old.txt',
                    mime_type=mime_type or 'text/plain',
                    version=expected_version + 1,
                )
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        UpdateGateway(),  # type: ignore[arg-type]
        file_store,  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_update_file',
            {
                'file_id': 'update_target_id',
                'expected_version': 5,
                'name': 'updated_name.txt',
                'managed_name': 'replacement.txt',
                'expected_size': 256,
                'expected_sha256': 'e' * 64,
                'mime_type': 'text/plain',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == (
        'update_target_id',
        5,
        'updated_name.txt',
        'replacement.txt',
        256,
        'e' * 64,
        'text/plain',
        file_store,
    )
    assert result.structured_content['file']['file_id'] == 'update_target_id'
    assert result.structured_content['file']['name'] == 'updated_name.txt'
    assert result.structured_content['file']['version'] == 6


@pytest.mark.asyncio
async def test_move_file_delegates_to_gateway() -> None:
    recorded_calls: list[tuple[str, int, str]] = []

    class MoveGateway(UnusedGateway):
        def move_file(
            self,
            file_id: str,
            expected_version: int,
            destination_parent_id: str,
        ) -> DriveMutationResult:
            recorded_calls.append(
                (
                    file_id,
                    expected_version,
                    destination_parent_id,
                )
            )
            return DriveMutationResult(
                file=DriveFile(
                    file_id=file_id,
                    name='moved.txt',
                    mime_type='text/plain',
                    version=expected_version + 1,
                    parents=(destination_parent_id,),
                )
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        MoveGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_move_file',
            {
                'file_id': 'move_target_id',
                'expected_version': 3,
                'destination_parent_id': 'new_folder_99',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == ('move_target_id', 3, 'new_folder_99')
    assert result.structured_content['file']['file_id'] == 'move_target_id'
    assert result.structured_content['file']['parents'] == ['new_folder_99']


@pytest.mark.asyncio
async def test_copy_file_delegates_to_gateway() -> None:
    recorded_calls: list[tuple[str, str | None, str | None]] = []

    class CopyGateway(UnusedGateway):
        def copy_file(
            self,
            file_id: str,
            name: str | None = None,
            parent_id: str | None = None,
        ) -> DriveMutationResult:
            recorded_calls.append((file_id, name, parent_id))
            return DriveMutationResult(
                file=DriveFile(
                    file_id='copied_file_id',
                    name=name or 'copy.txt',
                    mime_type='text/plain',
                    parents=(parent_id,) if parent_id else (),
                )
            )

    server = PolicyMCPServer('drive')
    register_drive_tools(
        ToolRegistrar(server),
        CopyGateway(),  # type: ignore[arg-type]
        UnusedFileStore(),  # type: ignore[arg-type]
    )

    principal = context.AuthenticatedPrincipal(
        principal_id='user_1',
        credential_id='0' * 64,
        client_id='client',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = context.set_request_context(principal, 'request')
    try:
        result = await server.call_tool(
            'drive_copy_file',
            {
                'file_id': 'source_file_id',
                'name': 'Custom Copy Name.txt',
                'parent_id': 'target_folder_id',
            },
        )
    finally:
        context.reset_request_context(token)

    assert len(recorded_calls) == 1
    assert recorded_calls[0] == (
        'source_file_id',
        'Custom Copy Name.txt',
        'target_folder_id',
    )
    assert result.structured_content['file']['file_id'] == 'copied_file_id'
    assert result.structured_content['file']['name'] == 'Custom Copy Name.txt'


@pytest.mark.asyncio
async def test_run_gateway_preserves_drive_errors() -> None:
    def failing_input() -> None:
        raise DriveInputError('invalid search text')

    def failing_scope() -> None:
        raise DriveScopeError('missing write permissions')

    def failing_conflict() -> None:
        raise DriveConflictError('file changed')

    def failing_provider() -> None:
        raise DriveProviderError('rate limited')

    with pytest.raises(DriveInputError, match='invalid search text'):
        await run_gateway(failing_input)

    with pytest.raises(DriveScopeError, match='missing write permissions'):
        await run_gateway(failing_scope)

    with pytest.raises(DriveConflictError, match='file changed'):
        await run_gateway(failing_conflict)

    with pytest.raises(DriveProviderError, match='rate limited'):
        await run_gateway(failing_provider)


@pytest.mark.asyncio
async def test_run_gateway_masks_unexpected_exceptions() -> None:
    def secret_error() -> None:
        raise RuntimeError('internal database connection string or secret')

    def type_error() -> None:
        raise TypeError('unexpected NoneType')

    with pytest.raises(
        DriveProviderError, match='^Drive returned an invalid response$'
    ):
        await run_gateway(secret_error)

    with pytest.raises(
        DriveProviderError, match='^Drive returned an invalid response$'
    ):
        await run_gateway(type_error)
