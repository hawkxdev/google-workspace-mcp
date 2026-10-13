"""Execute safe Gmail tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated

from pydantic import Field

from google_workspace_mcp.auth.context import current_request_context
from google_workspace_mcp.common.config import GmailReadBudget
from google_workspace_mcp.common.deadline import (
    OperationDeadline,
    bind_deadline,
)

from ..errors import GmailError, GmailProviderError

Recipient = Annotated[
    str,
    Field(
        min_length=3,
        max_length=320,
        description='One RFC 5322 mailbox',
    ),
]
_LOGGER = logging.getLogger(__name__)


async def run_gateway[ResultT](
    operation: Callable[..., ResultT],
    *args: object,
    read_budget: GmailReadBudget | None = None,
) -> ResultT:
    """Execute scrubbed Gmail operation."""
    try:
        if read_budget is None:
            return await asyncio.to_thread(operation, *args)
        context = current_request_context()
        deadline = OperationDeadline(
            read_budget.total_seconds,
            read_budget.attempt_seconds,
            read_budget.backoff_seconds,
            request_id=context.request_id if context else 'unavailable',
        )

        def bounded_operation() -> ResultT:
            """Release completed read resources."""
            try:
                deadline.remaining()
                return operation(*args)
            finally:
                for cleanup in deadline.cleanups:
                    cleanup()

        with bind_deadline(deadline):
            try:
                async with asyncio.timeout(read_budget.total_seconds):
                    result = await asyncio.to_thread(bounded_operation)
                    deadline.remaining()
                    return result
            except TimeoutError:
                _LOGGER.warning('%s', deadline.timeout())
                raise GmailProviderError(str(deadline.timeout())) from None
            finally:
                deadline.cancelled.set()
    except GmailError:
        raise
    except Exception:
        raise GmailProviderError(
            'Gmail returned an invalid response'
        ) from None
