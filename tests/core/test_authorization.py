from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from google_workspace_mcp.auth import context
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension


def _make_principal(
    *,
    principal_id: str = 'test:principal',
    capabilities: frozenset[str] = frozenset(),
    full_access: bool = False,
) -> context.AuthenticatedPrincipal:
    """Build authenticated test principal."""
    return context.AuthenticatedPrincipal(
        principal_id=principal_id,
        credential_id='0' * 64,
        client_id='test-client',
        policy='test-policy',
        capabilities=capabilities,
        full_access=full_access,
    )


@pytest.mark.asyncio
async def test_policy_server_guards_tools() -> None:
    srv = PolicyMCPServer('test-srv')
    reg = ToolRegistrar(srv)

    @reg.tool(name='op', required_capability='mail.read')
    def op() -> str:
        return 'ok'

    assert srv.required_capability('op') == 'mail.read'
    assert len(await srv.list_tools()) == 0
    with pytest.raises(ToolError, match='Forbidden'):
        await srv.call_tool('op', {})

    denied = _make_principal(capabilities=frozenset({'calendar.read'}))
    token = context.set_request_context(denied, 'req-1')
    try:
        assert len(await srv.list_tools()) == 0
        with pytest.raises(ToolError, match='Forbidden'):
            await srv.call_tool('op', {})
    finally:
        context.reset_request_context(token)

    allowed = _make_principal(capabilities=frozenset({'mail.read'}))
    token = context.set_request_context(allowed, 'req-2')
    try:
        tools = await srv.list_tools()
        assert [t.name for t in tools] == ['op']
        result = await srv.call_tool('op', {})
        assert result.content[0].text == 'ok'
    finally:
        context.reset_request_context(token)

    full = _make_principal(full_access=True)
    token = context.set_request_context(full, 'req-3')
    try:
        tools = await srv.list_tools()
        assert [t.name for t in tools] == ['op']
        result = await srv.call_tool('op', {})
        assert result.content[0].text == 'ok'
    finally:
        context.reset_request_context(token)


def test_policy_registrar_is_narrow_and_fail_closed() -> None:
    srv = PolicyMCPServer('registrar-test')
    reg = ToolRegistrar(srv)

    assert not hasattr(reg, 'resource')
    assert not hasattr(reg, 'prompt')

    with pytest.raises(TypeError, match='Use @tool'):
        reg.tool(lambda: 'err')  # type: ignore[arg-type]

    with pytest.raises(ValueError, match='must not be empty'):
        srv.register_capability('empty_cap_tool', '')

    @reg.tool(name='auto_cap_tool')
    def auto_cap_tool() -> str:
        return 'auto'

    assert srv.required_capability('auto_cap_tool') == 'auto_cap_tool'


@pytest.mark.asyncio
async def test_policy_server_guards_resources_and_prompts() -> None:
    srv = PolicyMCPServer('res-prompt-srv')

    @srv.resource('test://data')
    def get_data() -> str:
        return 'data'

    @srv.prompt(name='test_prompt')
    def get_test_prompt() -> str:
        return 'prompt text'

    assert await srv.list_resources() == []
    assert await srv.list_resource_templates() == []
    assert await srv.list_prompts() == []

    with pytest.raises(ResourceError, match='Forbidden'):
        await srv.read_resource('test://data')

    with pytest.raises(ValueError, match='Forbidden'):
        await srv.get_prompt('test_prompt')

    limited = _make_principal(capabilities=frozenset({'mail.read'}))
    token = context.set_request_context(limited, 'req-res-1')
    try:
        assert await srv.list_resources() == []
        assert await srv.list_resource_templates() == []
        assert await srv.list_prompts() == []

        with pytest.raises(ResourceError, match='Forbidden'):
            await srv.read_resource('test://data')

        with pytest.raises(ValueError, match='Forbidden'):
            await srv.get_prompt('test_prompt')
    finally:
        context.reset_request_context(token)

    full = _make_principal(full_access=True)
    token = context.set_request_context(full, 'req-res-2')
    try:
        resources = await srv.list_resources()
        assert len(resources) == 1
        assert str(resources[0].uri) == 'test://data'

        prompts = await srv.list_prompts()
        assert len(prompts) == 1
        assert prompts[0].name == 'test_prompt'

        read_res = await srv.read_resource('test://data')
        assert read_res[0].content == 'data'

        prompt_res = await srv.get_prompt('test_prompt')
        assert prompt_res.messages[0].content.text == 'prompt text'
    finally:
        context.reset_request_context(token)


@pytest.mark.asyncio
async def test_policy_server_unknown_tool_and_unclassified_tool() -> None:
    srv = PolicyMCPServer('unknown-srv')
    assert srv.required_capability('nonexistent') is None

    srv.add_tool(lambda: 'bare', name='bare_tool')
    assert srv.required_capability('bare_tool') is None

    assert await srv.list_tools() == []
    with pytest.raises(ToolError, match='Forbidden'):
        await srv.call_tool('bare_tool', {})

    full = _make_principal(full_access=True)
    token = context.set_request_context(full, 'req-bare')
    try:
        tools = await srv.list_tools()
        assert [t.name for t in tools] == ['bare_tool']
        result = await srv.call_tool('bare_tool', {})
        assert result.content[0].text == 'bare'
    finally:
        context.reset_request_context(token)


def test_extension_seam_base_class() -> None:
    class CustomExtension(Extension):
        def __init__(self) -> None:
            self.tools_registered = False
            self.routes_registered = False
            self.shut_down = False

        def register_tools(self, registrar: ToolRegistrar) -> None:
            self.tools_registered = True

        def register_routes(self, app: Any) -> None:
            self.routes_registered = True

        def shutdown(self) -> None:
            self.shut_down = True

    base = Extension()
    srv = PolicyMCPServer('ext-srv')
    reg = ToolRegistrar(srv)
    base.register_tools(reg)
    base.register_routes(None)  # type: ignore[arg-type]
    base.shutdown()

    ext = CustomExtension()
    ext.register_tools(reg)
    ext.register_routes(None)
    ext.shutdown()

    assert ext.tools_registered is True
    assert ext.routes_registered is True
    assert ext.shut_down is True
