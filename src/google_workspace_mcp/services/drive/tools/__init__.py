"""Drive MCP tools package."""

from google_workspace_mcp.common.managed_files import ManagedFileStore
from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DriveGateway
from .files import register_file_tools
from .read import register_drive_read_tools


def register_drive_tools(
    registrar: ToolRegistrar,
    gateway: DriveGateway,
    files: ManagedFileStore,
) -> None:
    """Register complete Drive tools."""
    register_drive_read_tools(registrar, gateway)
    register_file_tools(registrar, gateway, files)


__all__ = [
    'register_drive_read_tools',
    'register_drive_tools',
    'register_file_tools',
]
