"""Test Gmail extension wiring."""

from __future__ import annotations

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services.gmail import GMAIL_SCOPE, GmailExtension
from google_workspace_mcp.services.gmail.factory import create_gmail_app
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .test_gmail_tools import READONLY_TOOLS, TOOL_NAMES


class ExtraExtension(Extension):
    """Register one external tool."""

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register external test tool."""

        @registrar.tool(name='external_tool')
        def external_tool() -> str:
            """Provide external test tool."""
            return 'external'


def test_factory_registers_builtin_gmail_extension(
    service_config: ServiceConfig,
) -> None:
    app, server, state = create_gmail_app(
        service_config,
        extensions=(ExtraExtension(),),
    )
    try:
        names = {tool.name for tool in server._tool_manager.list_tools()}
        assert names == TOOL_NAMES | {'external_tool'}
        assert set(state.readonly_capabilities) == READONLY_TOOLS
        assert state.service_id == 'gmail'
        assert app is not None
    finally:
        state.close()


def test_extension_owns_service_specific_store(
    service_config: ServiceConfig,
) -> None:
    extension = GmailExtension(service_config)
    assert extension.store.path == service_config.google_token_path
    assert extension.store.required_scopes == (GMAIL_SCOPE,)
