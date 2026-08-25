"""Register all Calendar tools."""

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..batch import CalendarBatchExecutor
from ..client import CalendarGateway
from ..recurrence import RecurringEventMutator
from .batch import register_batch_tool
from .events import register_event_tools
from .read import register_read_tools


def register_calendar_tools(
    registrar: ToolRegistrar,
    gateway: CalendarGateway,
    recurring: RecurringEventMutator,
    batch: CalendarBatchExecutor,
) -> None:
    """Register complete Calendar tools."""
    register_read_tools(registrar, gateway)
    register_event_tools(registrar, gateway, recurring)
    register_batch_tool(registrar, batch)


__all__ = ['register_calendar_tools']
