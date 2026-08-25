"""Register all Gmail tools."""

from google_workspace_mcp.common.managed_files import ManagedFileStore
from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import GmailGateway
from .drafts import register_draft_tools
from .mutations import register_mutation_tools
from .read import register_read_tools
from .send import register_send_tools


def register_gmail_tools(
    registrar: ToolRegistrar,
    gateway: GmailGateway,
    attachments: ManagedFileStore,
) -> None:
    """Register complete Gmail tools."""
    register_read_tools(registrar, gateway)
    register_mutation_tools(registrar, gateway, attachments)
    register_draft_tools(registrar, gateway)
    register_send_tools(registrar, gateway)


__all__ = ['register_gmail_tools']
