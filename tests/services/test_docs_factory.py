"""Test Docs extension wiring."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services.docs import (
    DOCS_SCOPES,
    DocsExtension,
    create_docs_app,
)
from google_workspace_mcp.services.docs.client import DocsGateway
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension

from .test_docs_tools import (
    DOCS_TOOL_NAMES,
    READONLY_DOCS_TOOLS,
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


def docs_config(tmp_path: Path) -> ServiceConfig:
    """Create isolated Docs config."""
    root = tmp_path / 'docs'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    return ServiceConfig(
        service_id='docs',
        public_url='https://mcp.example.test/docs',
        mcp_path='/docs/mcp',
        host='127.0.0.1',
        port=8435,
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


def test_factory_registers_builtin_docs_extension(tmp_path: Path) -> None:
    config = docs_config(tmp_path)
    app, server, state = create_docs_app(
        config,
        extensions=(ExtraExtension(),),
    )
    try:
        names = {tool.name for tool in server._tool_manager.list_tools()}
        assert names == DOCS_TOOL_NAMES | {'external_tool'}
        assert len(DOCS_TOOL_NAMES) == 7
        assert set(state.readonly_capabilities) == READONLY_DOCS_TOOLS
        assert len(state.readonly_capabilities) == 2
        assert state.service_id == 'docs'
        assert app is not None
    finally:
        state.close()


def test_builtin_before_caller_extension_order(tmp_path: Path) -> None:
    config = docs_config(tmp_path)
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

    app, server, state = create_docs_app(
        config,
        extensions=(OrderTrackingExtension(),),
    )
    try:
        assert set(registered_order) == DOCS_TOOL_NAMES
        all_tools = [tool.name for tool in server._tool_manager.list_tools()]
        assert all_tools[-1] == 'custom_tail_tool'
    finally:
        state.close()


def test_extension_owns_docs_scopes_and_paths(tmp_path: Path) -> None:
    config = docs_config(tmp_path)
    extension = DocsExtension(config)
    assert extension.store.path == config.google_token_path
    assert extension.store.required_scopes == DOCS_SCOPES
    assert extension.store.required_scopes == (
        'https://www.googleapis.com/auth/documents',
        'https://www.googleapis.com/auth/drive.file',
    )
    assert len(extension.store.required_scopes) == 2


def test_factory_default_config_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / 'docs_env'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    for key, value in {
        'DOCS_MCP_PORT': '8435',
        'DOCS_MCP_PUBLIC_URL': 'https://mcp.example.test/docs',
        'DOCS_MCP_PATH': '/docs/mcp',
        'DOCS_OAUTH_STATE_PATH': str(root / 'oauth.db'),
        'DOCS_GOOGLE_TOKEN_PATH': str(root / 'token.json'),
        'DOCS_AUDIT_LOG_PATH': str(root / 'audit.jsonl'),
        'DOCS_MCP_DOWNLOAD_PATH': str(downloads),
        'DOCS_OAUTH_LOGIN_USERNAME': 'docs_admin',
        'DOCS_OAUTH_LOGIN_PASSWORD': 'docs_password',
    }.items():
        monkeypatch.setenv(key, value)

    app, server, state = create_docs_app()
    try:
        assert state.service_id == 'docs'
        assert {
            tool.name for tool in server._tool_manager.list_tools()
        } == DOCS_TOOL_NAMES
        assert set(state.readonly_capabilities) == READONLY_DOCS_TOOLS
        assert len(state.readonly_capabilities) == 2
    finally:
        state.close()


def test_docs_extension_custom_gateway_factory(tmp_path: Path) -> None:
    config = docs_config(tmp_path)
    gateway = FakeGateway()
    extension = DocsExtension(
        config,
        gateway_factory=lambda _store: cast(DocsGateway, gateway),
    )
    server = PolicyMCPServer('docs')
    extension.register_tools(ToolRegistrar(server))
    assert {
        tool.name for tool in server._tool_manager.list_tools()
    } == DOCS_TOOL_NAMES
