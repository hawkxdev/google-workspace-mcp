"""Own Gmail service tools."""

from __future__ import annotations

from collections.abc import Callable

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.common.managed_files import ManagedFileStore
from google_workspace_mcp.google_auth import GoogleCredentialStore
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .client import GmailGateway
from .constants import GMAIL_SCOPE, MAX_ATTACHMENT_BYTES
from .tools import register_gmail_tools

GatewayFactory = Callable[[GoogleCredentialStore], GmailGateway]


class GmailExtension(Extension):
    """Own Gmail service resources."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        gateway_factory: GatewayFactory = GmailGateway,
    ) -> None:
        """Initialize Gmail service extension."""
        self._store = GoogleCredentialStore(
            config.google_token_path,
            config.download_path,
            (GMAIL_SCOPE,),
        )
        self._gateway = gateway_factory(self._store)
        self._read_budget = config.gmail_read_budget
        self._attachments = ManagedFileStore(
            config.download_path,
            MAX_ATTACHMENT_BYTES,
        )

    @property
    def store(self) -> GoogleCredentialStore:
        """Return Gmail credential store."""
        return self._store

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register owned Gmail tools."""
        register_gmail_tools(
            registrar,
            self._gateway,
            self._attachments,
            self._read_budget,
        )
