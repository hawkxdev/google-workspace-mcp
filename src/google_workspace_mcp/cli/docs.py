"""Docs command line entrypoint."""

from collections.abc import Sequence

from google_workspace_mcp.cli.runner import run_service_server
from google_workspace_mcp.services.docs.factory import create_docs_app
from google_workspace_mcp.transport.extensions import Extension

SERVICE_NAME = 'docs'


def run_server(extensions: Sequence[Extension] = ()) -> None:
    """Run Docs service server."""
    run_service_server('docs', create_docs_app, extensions=extensions)


def main() -> None:
    """Execute main entrypoint function."""
    run_server()
