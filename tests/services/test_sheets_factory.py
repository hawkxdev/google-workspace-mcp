"""Test Sheets extension wiring."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services.sheets import (
    SHEETS_SCOPES,
    SheetsExtension,
    create_sheets_app,
)
from google_workspace_mcp.services.sheets.client import SheetsGateway
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension

from .test_sheets_tools import (
    READONLY_SHEETS_TOOLS,
    SHEETS_TOOL_NAMES,
    FakeGateway,
)


class ExtraExtension(Extension):
    """Register one external tool."""

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register external test tool."""

        @registrar.tool(name='external_tool')
        def external_tool() -> str:
            """Return external test string."""
            return 'external'


def _config(tmp_path: Path) -> ServiceConfig:
    """Create isolated Sheets config."""
    root = tmp_path / 'sheets'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    return ServiceConfig(
        service_id='sheets',
        public_url='https://127.0.0.1:8434',
        mcp_path='/sheets/mcp',
        host='127.0.0.1',
        port=8434,
        download_path=downloads,
        oauth_state_path=root / 'oauth.db',
        google_token_path=root / 'token.json',
        audit_log_path=root / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret_password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )


def test_factory_registers_builtin_sheets_extension(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app, server, state = create_sheets_app(
        config,
        extensions=(ExtraExtension(),),
    )
    try:
        tools = server._tool_manager.list_tools()
        names = {tool.name for tool in tools}
        assert names == SHEETS_TOOL_NAMES | {'external_tool'}
        assert len(SHEETS_TOOL_NAMES) == 11
        assert set(state.readonly_capabilities) == READONLY_SHEETS_TOOLS
        assert len(state.readonly_capabilities) == 3
        assert state.service_id == 'sheets'
        assert app is not None
    finally:
        state.close()


def test_builtin_before_caller_extension_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registered_order: list[str] = []

    class OrderTrackingExtension(Extension):
        """Track tool registration order."""

        def register_tools(self, registrar: ToolRegistrar) -> None:
            """Record tool registration order."""
            registered_order.extend(
                tool.name
                for tool in registrar._server._tool_manager.list_tools()
            )

            @registrar.tool(name='custom_tail_tool')
            def custom_tail_tool() -> str:
                """Return custom tail string."""
                return 'tail'

    app, server, state = create_sheets_app(
        config,
        extensions=(OrderTrackingExtension(),),
    )
    try:
        assert set(registered_order) == SHEETS_TOOL_NAMES
        all_tools = [tool.name for tool in server._tool_manager.list_tools()]
        assert all_tools[-1] == 'custom_tail_tool'
    finally:
        state.close()


def test_extension_owns_sheets_scopes_and_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    extension = SheetsExtension(config)
    assert extension.store.path == config.google_token_path
    assert extension.store.required_scopes == SHEETS_SCOPES
    assert extension.store.required_scopes == (
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive.file',
    )
    assert len(extension.store.required_scopes) == 2


def test_factory_default_config_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / 'sheets_env'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    for k, v in {
        'SHEETS_MCP_PORT': '8434',
        'SHEETS_MCP_PUBLIC_URL': 'https://127.0.0.1:8434',
        'SHEETS_MCP_PATH': '/sheets/mcp',
        'SHEETS_OAUTH_STATE_PATH': str(root / 'oauth.db'),
        'SHEETS_GOOGLE_TOKEN_PATH': str(root / 'token.json'),
        'SHEETS_AUDIT_LOG_PATH': str(root / 'audit.jsonl'),
        'SHEETS_MCP_DOWNLOAD_PATH': str(downloads),
        'SHEETS_OAUTH_LOGIN_USERNAME': 'sheets_admin',
        'SHEETS_OAUTH_LOGIN_PASSWORD': 'sheets_password',
    }.items():
        monkeypatch.setenv(k, v)

    app, server, state = create_sheets_app()
    try:
        assert state.service_id == 'sheets'
        assert {
            tool.name for tool in server._tool_manager.list_tools()
        } == SHEETS_TOOL_NAMES
        assert set(state.readonly_capabilities) == READONLY_SHEETS_TOOLS
        assert len(state.readonly_capabilities) == 3
    finally:
        state.close()


def test_sheets_extension_custom_gateway_factory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    gateway = FakeGateway()
    extension = SheetsExtension(
        config,
        gateway_factory=lambda _store: cast(SheetsGateway, gateway),
    )
    server = PolicyMCPServer('sheets')
    extension.register_tools(ToolRegistrar(server))
    assert {
        tool.name for tool in server._tool_manager.list_tools()
    } == SHEETS_TOOL_NAMES
