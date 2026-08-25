"""Support safe Sheets tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mcp.server.mcpserver.exceptions import ToolError

from ..errors import SheetsError


async def run_gateway[**P, R](
    function: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run Sheets gateway call."""
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except SheetsError as error:
        raise ToolError(str(error)) from error
