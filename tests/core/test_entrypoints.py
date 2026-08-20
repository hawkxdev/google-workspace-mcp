"""Entrypoint contract tests."""

import importlib

import pytest

from google_workspace_mcp.cli import SERVICES

# === Service entrypoint cases ===


def test_five_services_are_declared() -> None:
    assert SERVICES == ('gmail', 'calendar', 'drive', 'sheets', 'docs')


@pytest.mark.parametrize('service', SERVICES)
def test_entrypoint_module_exposes_callable_main(service: str) -> None:
    module = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    assert service == module.SERVICE_NAME
    assert callable(module.main)


@pytest.mark.parametrize('service', SERVICES)
def test_entrypoint_reports_unavailable_runtime(service: str) -> None:
    module = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    with pytest.raises(
        SystemExit,
        match=rf'^{service}: service runtime is not implemented$',
    ):
        module.main()


# === SDK compatibility cases ===


def test_sdk_server_contract_is_present() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    assert callable(MCPServer)
    assert issubclass(ToolError, Exception)


def test_stdlib_is_not_shadowed_by_service_modules() -> None:
    import calendar as stdlib_calendar

    assert hasattr(stdlib_calendar, 'monthrange')
