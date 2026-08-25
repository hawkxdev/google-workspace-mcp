"""Execute safe Gmail tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated

from pydantic import Field

from ..errors import GmailError, GmailProviderError

Recipient = Annotated[
    str,
    Field(
        min_length=3,
        max_length=320,
        description='One RFC 5322 mailbox',
    ),
]


async def run_gateway[ResultT](
    operation: Callable[..., ResultT],
    *args: object,
) -> ResultT:
    """Execute scrubbed Gmail operation."""
    try:
        return await asyncio.to_thread(operation, *args)
    except GmailError:
        raise
    except Exception:
        raise GmailProviderError(
            'Gmail returned an invalid response'
        ) from None
