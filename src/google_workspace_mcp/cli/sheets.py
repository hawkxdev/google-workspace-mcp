"""Sheets command line entrypoint."""

from collections.abc import Sequence

from google_workspace_mcp.cli.runner import run_service_server
from google_workspace_mcp.services.sheets.factory import create_sheets_app
from google_workspace_mcp.transport.extensions import Extension

SERVICE_NAME = 'sheets'


def run_server(extensions: Sequence[Extension] = ()) -> None:
    """Run Sheets service server."""
    run_service_server('sheets', create_sheets_app, extensions=extensions)


def main() -> None:
    """Execute main entrypoint function."""
    run_server()
