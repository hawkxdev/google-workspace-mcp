"""Own Sheets service tools."""

from __future__ import annotations

from collections.abc import Callable

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.google_auth import GoogleCredentialStore
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .client import SheetsGateway
from .constants import SHEETS_SCOPES
from .tools import register_sheets_tools

GatewayFactory = Callable[[GoogleCredentialStore], SheetsGateway]


class SheetsExtension(Extension):
    """Own Sheets service resources."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        gateway_factory: GatewayFactory = SheetsGateway,
    ) -> None:
        """Initialize Sheets service extension."""
        self._store = GoogleCredentialStore(
            config.google_token_path,
            config.download_path,
            SHEETS_SCOPES,
        )
        self._gateway = gateway_factory(self._store)

    @property
    def store(self) -> GoogleCredentialStore:
        """Return Sheets credential store."""
        return self._store

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register owned Sheets tools."""
        register_sheets_tools(registrar, self._gateway)
