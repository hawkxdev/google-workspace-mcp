"""Own Drive service tools."""

from __future__ import annotations

from collections.abc import Callable

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.common.managed_files import ManagedFileStore
from google_workspace_mcp.google_auth import GoogleCredentialStore
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .client import DriveGateway
from .constants import DRIVE_SCOPES, MAX_DRIVE_DOWNLOAD_BYTES
from .tools import register_drive_tools

GatewayFactory = Callable[[GoogleCredentialStore], DriveGateway]


class DriveExtension(Extension):
    """Own Drive service resources."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        gateway_factory: GatewayFactory = DriveGateway,
    ) -> None:
        """Initialize Drive service extension."""
        self._store = GoogleCredentialStore(
            config.google_token_path,
            config.download_path,
            DRIVE_SCOPES,
        )
        self._gateway = gateway_factory(self._store)
        self._files = ManagedFileStore(
            config.download_path,
            MAX_DRIVE_DOWNLOAD_BYTES,
        )

    @property
    def store(self) -> GoogleCredentialStore:
        """Return Drive credential store."""
        return self._store

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register owned Drive tools."""
        register_drive_tools(
            registrar,
            self._gateway,
            self._files,
        )
