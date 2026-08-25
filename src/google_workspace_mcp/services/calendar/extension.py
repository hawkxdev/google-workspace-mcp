"""Own Calendar service tools."""

from __future__ import annotations

from collections.abc import Callable

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.google_auth import GoogleCredentialStore
from google_workspace_mcp.transport.authorization import ToolRegistrar
from google_workspace_mcp.transport.extensions import Extension

from .batch import CalendarBatchExecutor
from .client import CalendarGateway
from .constants import CALENDAR_SCOPES
from .recurrence import RecurringEventMutator
from .tools import register_calendar_tools

GatewayFactory = Callable[[GoogleCredentialStore], CalendarGateway]


class CalendarExtension(Extension):
    """Own Calendar service resources."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        gateway_factory: GatewayFactory = CalendarGateway,
    ) -> None:
        """Initialize Calendar service extension."""
        self._store = GoogleCredentialStore(
            config.google_token_path,
            config.download_path,
            CALENDAR_SCOPES,
        )
        self._gateway = gateway_factory(self._store)
        self._recurring = RecurringEventMutator(self._gateway)
        self._batch = CalendarBatchExecutor(self._gateway)

    @property
    def store(self) -> GoogleCredentialStore:
        """Return Calendar credential store."""
        return self._store

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register owned Calendar tools."""
        register_calendar_tools(
            registrar,
            self._gateway,
            self._recurring,
            self._batch,
        )
