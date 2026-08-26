"""Register Docs batch tool."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DocsGateway
from ..constants import MAX_DOCS_BATCH_OPERATIONS
from ..schemas import DocsBatchOperation, DocsBatchResult
from .common import run_gateway
from .read import DocumentId, TabId
from .text import RevisionId


def register_batch_tools(
    registrar: ToolRegistrar,
    gateway: DocsGateway,
) -> None:
    """Register Docs batch tool."""
    mutating = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @registrar.tool(
        name='docs_batch_update',
        title='Apply Document Batch',
        description=(
            'Apply up to twenty typed operations to one explicit tab in a '
            'single atomic request under the supplied revision. The '
            'provider runs them in the given order, so later operations '
            'observe earlier index shifts, while every index is validated '
            'against the supplied revision only. Supply indices for the '
            'state described by that revision, and split work into '
            'successive calls when an operation depends on an earlier '
            'shift. Replacement cannot be combined with operations that '
            'shift indices. Raw provider requests are refused.'
        ),
        annotations=mutating,
        structured_output=True,
    )
    async def docs_batch_update(
        document_id: DocumentId,
        tab_id: TabId,
        required_revision_id: RevisionId,
        operations: Annotated[
            list[DocsBatchOperation],
            Field(
                min_length=1,
                max_length=MAX_DOCS_BATCH_OPERATIONS,
                description='Typed operations applied in caller order',
            ),
        ],
    ) -> DocsBatchResult:
        """Apply typed atomic batch."""
        return await run_gateway(
            gateway.batch_update,
            document_id,
            tab_id,
            operations,
            required_revision_id=required_revision_id,
        )
