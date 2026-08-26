"""Register Docs read tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DocsGateway
from ..constants import (
    MAX_DOCS_BLOCKS,
    MAX_DOCS_ID_CHARS,
    MAX_DOCS_OUTPUT_CHARS,
)
from ..schemas import DocsContentResponse, DocumentSummary
from .common import run_gateway

DocumentId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_DOCS_ID_CHARS,
        description='Google Docs document identifier',
    ),
]

TabId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_DOCS_ID_CHARS,
        description='Explicit document tab identifier',
    ),
]


def register_read_tools(
    registrar: ToolRegistrar,
    gateway: DocsGateway,
) -> None:
    """Register Docs read tools."""
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @registrar.tool(
        name='docs_get_document',
        title='Get Document Metadata',
        description=(
            'Get the title, revision and recursive tab tree of a Google '
            'Document. Returns no body content. Use the returned revision '
            'for any following mutation.'
        ),
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def docs_get_document(document_id: DocumentId) -> DocumentSummary:
        """Get document tab metadata."""
        return await run_gateway(gateway.get_document, document_id)

    @registrar.tool(
        name='docs_read_content',
        title='Read Document Tab Content',
        description=(
            'Read bounded typed content of one explicit document tab. '
            'Indices are UTF-16 code units and ranges are half open. '
            'Unsupported structures are reported, never flattened.'
        ),
        annotations=readonly,
        structured_output=True,
        available_to_readonly=True,
    )
    async def docs_read_content(
        document_id: DocumentId,
        tab_id: TabId,
        start_index: Annotated[
            int | None,
            Field(
                default=None,
                ge=0,
                description='Optional inclusive UTF-16 start index',
            ),
        ] = None,
        end_index: Annotated[
            int | None,
            Field(
                default=None,
                ge=0,
                description='Optional exclusive UTF-16 end index',
            ),
        ] = None,
        max_blocks: Annotated[
            int,
            Field(
                default=MAX_DOCS_BLOCKS,
                ge=1,
                le=MAX_DOCS_BLOCKS,
                description='Maximum structural blocks to return',
            ),
        ] = MAX_DOCS_BLOCKS,
        max_chars: Annotated[
            int,
            Field(
                default=MAX_DOCS_OUTPUT_CHARS,
                ge=1,
                le=MAX_DOCS_OUTPUT_CHARS,
                description='Maximum text characters to return',
            ),
        ] = MAX_DOCS_OUTPUT_CHARS,
    ) -> DocsContentResponse:
        """Read bounded tab content."""
        return await run_gateway(
            gateway.read_content,
            document_id,
            tab_id,
            start_index=start_index,
            end_index=end_index,
            max_blocks=max_blocks,
            max_chars=max_chars,
        )
