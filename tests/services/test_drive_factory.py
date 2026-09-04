"""Test Drive extension wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services.drive import (
    DRIVE_SCOPES,
    DriveExtension,
    create_drive_app,
)
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .test_drive_tools import READONLY_TOOL_NAMES, TOOL_NAMES


class ExtraExtension(Extension):
    """Register one external tool."""

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register test tools."""

        @registrar.tool(name='external_tool')
        def external_tool() -> str:
            """Provide external test tool."""
            return 'external'


def _config(tmp_path: Path) -> ServiceConfig:
    """Provide service configuration."""
    root = tmp_path / 'drive'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    return ServiceConfig(
        service_id='drive',
        public_url='https://mcp.example.test/drive',
        mcp_path='/drive/mcp',
        host='127.0.0.1',
        port=8433,
        download_path=downloads,
        oauth_state_path=root / 'oauth.db',
        google_token_path=root / 'token.json',
        audit_log_path=root / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )


def test_factory_registers_builtin_drive_extension(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app, server, state = create_drive_app(
        config,
        extensions=(ExtraExtension(),),
    )
    try:
        tools = server._tool_manager.list_tools()
        names = {tool.name for tool in tools}
        assert names == TOOL_NAMES | {'external_tool'}
        assert len(TOOL_NAMES) == 10
        assert set(state.readonly_capabilities) == READONLY_TOOL_NAMES
        assert len(state.readonly_capabilities) == 3
        assert state.service_id == 'drive'
        assert app is not None
    finally:
        state.close()


def test_builtin_before_caller_extension_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registered_order: list[str] = []

    class OrderTrackingExtension(Extension):
        """Model test extension."""

        def register_tools(self, registrar: ToolRegistrar) -> None:
            """Register test tools."""
            registered_order.extend(
                tool.name
                for tool in registrar._server._tool_manager.list_tools()
            )

            @registrar.tool(name='custom_tail_tool')
            def custom_tail_tool() -> str:
                """Provide trailing test tool."""
                return 'tail'

    app, server, state = create_drive_app(
        config,
        extensions=(OrderTrackingExtension(),),
    )
    try:
        assert set(registered_order) == TOOL_NAMES
        all_tools = [tool.name for tool in server._tool_manager.list_tools()]
        assert all_tools[-1] == 'custom_tail_tool'
    finally:
        state.close()


def test_extension_owns_drive_scopes_and_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    extension = DriveExtension(config)
    assert extension.store.path == config.google_token_path
    assert extension.store.required_scopes == DRIVE_SCOPES
    assert extension.store.required_scopes == (
        'https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/drive.file',
    )
    assert extension._files.directory == config.download_path
    assert extension._files.max_bytes == 25 * 1024 * 1024


def test_factory_default_config_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / 'drive_env'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    for k, v in {
        'DRIVE_MCP_PORT': '8433',
        'DRIVE_MCP_PUBLIC_URL': 'https://mcp.example.test/drive',
        'DRIVE_MCP_PATH': '/drive/mcp',
        'DRIVE_OAUTH_STATE_PATH': str(root / 'oauth.db'),
        'DRIVE_GOOGLE_TOKEN_PATH': str(root / 'token.json'),
        'DRIVE_AUDIT_LOG_PATH': str(root / 'audit.jsonl'),
        'DRIVE_MCP_DOWNLOAD_PATH': str(downloads),
        'DRIVE_OAUTH_LOGIN_USERNAME': 'drive-admin',
        'DRIVE_OAUTH_LOGIN_PASSWORD': 'drive-password',
    }.items():
        monkeypatch.setenv(k, v)

    app, server, state = create_drive_app()
    try:
        assert state.service_id == 'drive'
        assert {
            tool.name for tool in server._tool_manager.list_tools()
        } == TOOL_NAMES
        assert set(state.readonly_capabilities) == READONLY_TOOL_NAMES
        assert len(state.readonly_capabilities) == 3
    finally:
        state.close()
