"""Контракт импорта точек входа и границы SDK."""

import importlib

import pytest

from google_workspace_mcp.cli import SERVICES


def test_five_services_are_declared() -> None:
    assert SERVICES == ('gmail', 'calendar', 'drive', 'sheets', 'docs')


@pytest.mark.parametrize('service', SERVICES)
def test_entrypoint_module_exposes_callable_main(service: str) -> None:
    module = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    assert service == module.SERVICE_NAME
    assert callable(module.main)


def test_sdk_server_contract_is_present() -> None:
    """Пин имени, объявленного в зависимостях: mcp 2.x."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    assert callable(MCPServer)
    assert issubclass(ToolError, Exception)


def test_stdlib_is_not_shadowed_by_service_modules() -> None:
    """calendar и docs совпадают с именами вне пакета."""
    import calendar as stdlib_calendar

    assert hasattr(stdlib_calendar, 'monthrange')
