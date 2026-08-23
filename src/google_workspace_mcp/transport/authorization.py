"""Transport authorization policy module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from google_workspace_mcp.auth.context import current_principal


class PolicyMCPServer(MCPServer):
    """Server enforcing tool capabilities."""

    def __init__(self, name: str | None = None, **kwargs: Any) -> None:
        """Initialize policy server instance."""
        self._required_tool_capabilities: dict[str, str] = {}
        super().__init__(name=name, **kwargs)

    def register_capability(self, name: str, capability: str) -> None:
        """Register tool capability requirement."""
        if not capability:
            raise ValueError('tool capability must not be empty')
        self._required_tool_capabilities[name] = capability

    def record_tool_capability(self, tool_name: str, capability: str) -> None:
        """Record tool capability requirement."""
        self.register_capability(tool_name, capability)

    def required_capability(self, tool_name: str) -> str | None:
        """Retrieve required tool capability."""
        return self._required_tool_capabilities.get(tool_name)

    def registered_capabilities(self) -> tuple[str, ...]:
        """List registered tool capabilities."""
        return tuple(sorted(set(self._required_tool_capabilities.values())))

    def _tool_allowed(self, tool_name: str) -> bool:
        """Check tool permission status."""
        principal = current_principal()
        if principal is None:
            return False
        if principal.full_access:
            return True
        required = self.required_capability(tool_name)
        return required is not None and required in principal.capabilities

    @staticmethod
    def _full_access() -> bool:
        """Check full access status."""
        principal = current_principal()
        return principal is not None and principal.full_access

    async def list_tools(self) -> list[Any]:
        """List permitted principal tools."""
        tools = await super().list_tools()
        return [tool for tool in tools if self._tool_allowed(tool.name)]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> Any:
        """Execute authorized tool call."""
        if not self._tool_allowed(name):
            raise ToolError(
                'Forbidden: tool is not permitted for this principal'
            )
        return await super().call_tool(name, arguments, context=context)

    async def list_resources(self) -> list[Any]:
        """List permitted server resources."""
        return await super().list_resources() if self._full_access() else []

    async def list_resource_templates(self) -> list[Any]:
        """List permitted resource templates."""
        return (
            await super().list_resource_templates()
            if self._full_access()
            else []
        )

    async def read_resource(self, uri: Any, context: Any = None) -> Any:
        """Read authorized resource contents."""
        if not self._full_access():
            raise ResourceError(
                'Forbidden: resources are not permitted for this principal'
            )
        return await super().read_resource(uri, context=context)

    async def list_prompts(self) -> list[Any]:
        """List permitted server prompts."""
        return await super().list_prompts() if self._full_access() else []

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: Any = None,
    ) -> Any:
        """Render authorized prompt template."""
        if not self._full_access():
            raise ValueError(
                'Forbidden: prompts are not permitted for this principal'
            )
        return await super().get_prompt(name, arguments, context=context)


class ToolRegistrar:
    """Registrar for tool capabilities."""

    __slots__ = ('_server',)

    def __init__(self, server: PolicyMCPServer) -> None:
        """Initialize tool registrar instance."""
        self._server = server

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: Any | None = None,
        icons: list[Any] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
        *,
        required_capability: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator registering tool capability."""
        if callable(name):
            raise TypeError(
                'Use @tool() rather than @tool when registering a tool'
            )

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            effective_name = name or fn.__name__
            capability = required_capability or effective_name
            self._server.add_tool(
                fn,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                icons=icons,
                meta=meta,
                structured_output=structured_output,
            )
            self._server.register_capability(effective_name, capability)
            return fn

        return decorator
