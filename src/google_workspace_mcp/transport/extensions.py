"""Extension seam for transport."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from .authorization import ToolRegistrar


class Extension:
    """Base class for extensions."""

    def register_tools(self, registrar: ToolRegistrar) -> None:
        """Register tools via registrar."""

    def register_routes(self, app: Starlette) -> None:
        """Register routes on app."""

    def shutdown(self) -> None:
        """Release resources on shutdown."""
