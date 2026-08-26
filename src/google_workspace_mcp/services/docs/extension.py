"""Own Docs service tools."""

from __future__ import annotations

from collections.abc import Callable

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.google_auth import GoogleCredentialStore
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .client import DocsGateway
from .constants import DOCS_SCOPES
from .tools import register_docs_tools

GatewayFactory = Callable[[GoogleCredentialStore], DocsGateway]


class DocsExtension(Extension):
    """Own Docs service resources."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        gateway_factory: GatewayFactory = DocsGateway,
    ) -> None:
        """Initialize Docs service extension."""
        self._store = GoogleCredentialStore(
            config.google_token_path,
            config.download_path,
            DOCS_SCOPES,
        )
        self._gateway = gateway_factory(self._store)

    @property
    def store(self) -> GoogleCredentialStore:
        """Return Docs credential store."""
        return self._store

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register owned Docs tools."""
        register_docs_tools(registrar, self._gateway)
