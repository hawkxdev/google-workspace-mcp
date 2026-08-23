"""Drive command line entrypoint."""

from collections.abc import Sequence

from google_workspace_mcp.cli.runner import run_service_server
from google_workspace_mcp.services.drive.factory import create_drive_app
from google_workspace_mcp.transport.extensions import Extension

SERVICE_NAME = 'drive'


def run_server(extensions: Sequence[Extension] = ()) -> None:
    """Run Drive service server."""
    run_service_server('drive', create_drive_app, extensions=extensions)


def main() -> None:
    """Execute main entrypoint function."""
    run_server()
