"""Support safe Drive tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..errors import DriveError, DriveProviderError


async def run_gateway[**P, T](
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Execute scrubbed Drive operation."""
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except DriveError:
        raise
    except Exception:
        raise DriveProviderError(
            'Drive returned an invalid response'
        ) from None
