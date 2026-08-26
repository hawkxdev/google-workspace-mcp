"""Register all Docs tools."""

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DocsGateway
from .batch import register_batch_tools
from .read import register_read_tools
from .text import register_text_tools


def register_docs_tools(
    registrar: ToolRegistrar,
    gateway: DocsGateway,
) -> None:
    """Register complete Docs tools."""
    register_read_tools(registrar, gateway)
    register_text_tools(registrar, gateway)
    register_batch_tools(registrar, gateway)


__all__ = ['register_docs_tools']
