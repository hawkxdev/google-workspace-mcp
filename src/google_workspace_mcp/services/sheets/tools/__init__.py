"""Register all Sheets tools."""

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import SheetsGateway
from .read import register_read_tools
from .structure import register_structure_tools
from .values import register_value_tools


def register_sheets_tools(
    registrar: ToolRegistrar,
    gateway: SheetsGateway,
) -> None:
    """Register complete Sheets tools."""
    register_read_tools(registrar, gateway)
    register_value_tools(registrar, gateway)
    register_structure_tools(registrar, gateway)


__all__ = ['register_sheets_tools']
