import importlib
from unittest.mock import MagicMock

import pytest

from google_workspace_mcp.cli import SERVICES


def test_five_services_are_declared() -> None:
    assert SERVICES == ('gmail', 'calendar', 'drive', 'sheets', 'docs')


@pytest.mark.parametrize('service', SERVICES)
def test_entrypoint_module_exposes_callables(service: str) -> None:
    module = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    assert service == module.SERVICE_NAME
    assert callable(module.main)
    assert callable(module.run_server)


@pytest.mark.parametrize('service', SERVICES)
def test_entrypoint_main_invokes_run_server(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    module = importlib.import_module(f'google_workspace_mcp.cli.{service}')
    mock_run_server = MagicMock()
    monkeypatch.setattr(module, 'run_server', mock_run_server)
    module.main()
    mock_run_server.assert_called_once_with()


def test_sdk_server_contract_is_present() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    assert callable(MCPServer)
    assert issubclass(ToolError, Exception)


def test_stdlib_is_not_shadowed_by_service_modules() -> None:
    import calendar as stdlib_calendar

    assert hasattr(stdlib_calendar, 'monthrange')
