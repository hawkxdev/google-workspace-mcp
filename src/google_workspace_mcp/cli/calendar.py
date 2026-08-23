"""Calendar command line entrypoint."""

from collections.abc import Sequence

from google_workspace_mcp.cli.runner import run_service_server
from google_workspace_mcp.services.calendar.factory import (
    create_calendar_app,
)
from google_workspace_mcp.transport.extensions import Extension

SERVICE_NAME = 'calendar'


def run_server(extensions: Sequence[Extension] = ()) -> None:
    """Run Calendar service server."""
    run_service_server('calendar', create_calendar_app, extensions=extensions)


def main() -> None:
    """Execute main entrypoint function."""
    run_server()
