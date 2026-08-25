"""Register Calendar batch tool."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..batch import CalendarBatchExecutor
from ..constants import MAX_BATCH_OPERATIONS
from ..schemas import BatchMutationResponse, BatchOperation
from .common import run_gateway


def register_batch_tool(
    registrar: ToolRegistrar,
    executor: CalendarBatchExecutor,
) -> None:
    """Register Calendar batch tool."""

    @registrar.tool(
        name='calendar_batch_mutate_events',
        title='Batch Mutate Calendar Events',
        description='Run bounded event mutations with per-item results.',
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def calendar_batch_mutate_events(
        operations: Annotated[
            list[BatchOperation],
            Field(min_length=1, max_length=MAX_BATCH_OPERATIONS),
        ],
    ) -> BatchMutationResponse:
        """Run bounded Calendar mutations."""
        return await run_gateway(executor.execute, operations)
