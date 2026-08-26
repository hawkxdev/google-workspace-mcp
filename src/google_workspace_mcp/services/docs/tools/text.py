"""Register Docs text tools."""

from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from google_workspace_mcp.transport.authorization import ToolRegistrar

from ..client import DocsGateway
from ..constants import (
    MAX_DOCS_ID_CHARS,
    MAX_DOCS_REPLACEMENTS,
    MAX_DOCS_TEXT_CHARS,
    MAX_DOCS_TITLE_CHARS,
)
from ..schemas import (
    DocsCreateResult,
    DocsMutationResult,
    DocsReplaceResult,
)
from .common import run_gateway
from .read import DocumentId, TabId

RevisionId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_DOCS_ID_CHARS,
        description=(
            'Revision returned by the most recent read or mutation. '
            'A stale revision is refused before any change is applied.'
        ),
    ),
]


def register_text_tools(
    registrar: ToolRegistrar,
    gateway: DocsGateway,
) -> None:
    """Register Docs text tools."""
    creating = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
    mutating = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @registrar.tool(
        name='docs_create_document',
        title='Create Document',
        description=(
            'Create an empty Google Document in Drive root and return its '
            'identifier, first tab and revision.'
        ),
        annotations=creating,
        structured_output=True,
    )
    async def docs_create_document(
        title: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DOCS_TITLE_CHARS,
                description='Title of the new document',
            ),
        ],
    ) -> DocsCreateResult:
        """Create empty Google document."""
        return await run_gateway(gateway.create_document, title)

    @registrar.tool(
        name='docs_insert_text',
        title='Insert Document Text',
        description=(
            'Insert text at one UTF-16 index inside an explicit tab under '
            'the supplied revision. Splitting a surrogate pair is refused.'
        ),
        annotations=mutating,
        structured_output=True,
    )
    async def docs_insert_text(
        document_id: DocumentId,
        tab_id: TabId,
        index: Annotated[
            int,
            Field(ge=0, description='UTF-16 insertion index'),
        ],
        text: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DOCS_TEXT_CHARS,
                description='Text to insert',
            ),
        ],
        required_revision_id: RevisionId,
    ) -> DocsMutationResult:
        """Insert text into tab."""
        return await run_gateway(
            gateway.insert_text,
            document_id,
            tab_id,
            index,
            text,
            required_revision_id=required_revision_id,
        )

    @registrar.tool(
        name='docs_delete_range',
        title='Delete Document Range',
        description=(
            'Delete a half open UTF-16 range inside an explicit tab. The '
            'mandatory final newline of the tab cannot be deleted.'
        ),
        annotations=mutating,
        structured_output=True,
    )
    async def docs_delete_range(
        document_id: DocumentId,
        tab_id: TabId,
        start_index: Annotated[
            int,
            Field(ge=0, description='Inclusive UTF-16 start index'),
        ],
        end_index: Annotated[
            int,
            Field(ge=0, description='Exclusive UTF-16 end index'),
        ],
        required_revision_id: RevisionId,
    ) -> DocsMutationResult:
        """Delete range from tab."""
        return await run_gateway(
            gateway.delete_range,
            document_id,
            tab_id,
            start_index,
            end_index,
            required_revision_id=required_revision_id,
        )

    @registrar.tool(
        name='docs_replace_text',
        title='Replace Document Text',
        description=(
            'Replace every occurrence of a single line literal inside one '
            'explicit tab. The expected occurrence count is verified before '
            'any change is applied.'
        ),
        annotations=mutating,
        structured_output=True,
    )
    async def docs_replace_text(
        document_id: DocumentId,
        tab_id: TabId,
        search_text: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DOCS_TEXT_CHARS,
                description='Single line literal to find',
            ),
        ],
        replacement_text: Annotated[
            str,
            Field(
                max_length=MAX_DOCS_TEXT_CHARS,
                description='Replacement text',
            ),
        ],
        required_revision_id: RevisionId,
        match_case: Annotated[
            bool,
            Field(description='Whether matching is case sensitive'),
        ],
        expected_occurrences: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_DOCS_REPLACEMENTS,
                description='Exact number of matches the caller expects',
            ),
        ],
    ) -> DocsReplaceResult:
        """Replace literal inside tab."""
        return await run_gateway(
            gateway.replace_text,
            document_id,
            tab_id,
            search_text,
            replacement_text,
            required_revision_id=required_revision_id,
            match_case=match_case,
            expected_occurrences=expected_occurrences,
        )
