"""Gmail command line entrypoint."""

from collections.abc import Sequence

from google_workspace_mcp.cli.runner import run_service_server
from google_workspace_mcp.services.gmail.factory import create_gmail_app
from google_workspace_mcp.transport.extensions import Extension

SERVICE_NAME = 'gmail'


def run_server(extensions: Sequence[Extension] = ()) -> None:
    """Run Gmail service server."""
    run_service_server('gmail', create_gmail_app, extensions=extensions)


def main() -> None:
    """Execute main entrypoint function."""
    run_server()
