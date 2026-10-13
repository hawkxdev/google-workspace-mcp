"""Verify shared tool invocation audit."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from google_workspace_mcp.audit.logger import AuditError, AuditLogger
from google_workspace_mcp.auth.context import (
    AuthenticatedPrincipal,
    reset_request_context,
    set_request_context,
)
from google_workspace_mcp.auth.state import MCP_READONLY_V1
from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.transport.authorization import (
    PolicyMCPServer,
    ToolRegistrar,
)
from google_workspace_mcp.transport.extensions import Extension
from google_workspace_mcp.transport.factory import create_service_app

from .test_audit_propagation import service_config as service_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'service', ['gmail', 'calendar', 'drive', 'sheets', 'docs']
)
@pytest.mark.parametrize(
    'outcome', ['success', 'tool_error', 'policy_denial', 'unknown_tool']
)
async def test_one_scrubbed_audit_event_per_call(
    tmp_path: Path, service: str, outcome: str
) -> None:
    audit = AuditLogger(tmp_path / 'audit' / 'events.jsonl')
    server = PolicyMCPServer(service)
    server.set_tool_audit(service, audit.log_event)
    registrar = ToolRegistrar(server)

    @registrar.tool(name='sample_tool')
    def sample_tool(content: str) -> str:
        """Return synthetic sensitive content."""
        if outcome == 'tool_error':
            raise ValueError('private result address@example.com')
        return 'private result address@example.com'

    principal = AuthenticatedPrincipal(
        principal_id='principal',
        credential_id='credential',
        client_id='client-id',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=outcome != 'policy_denial',
    )
    token = set_request_context(principal, 'authenticated-request')
    name = (
        'argument@example.com' if outcome == 'unknown_tool' else 'sample_tool'
    )
    try:
        if outcome == 'success':
            result = await server.call_tool(name, {'content': 'private input'})
            assert (
                result.content[0].text == 'private result address@example.com'
            )
        else:
            with pytest.raises(ToolError):
                await server.call_tool(name, {'content': 'private input'})
    finally:
        reset_request_context(token)
    lines = (tmp_path / 'audit/events.jsonl').read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert set(event) == {
        'timestamp',
        'operation',
        'service',
        'tool_name',
        'request_id',
        'elapsed_seconds',
        'client_id',
        'auth_policy',
        'operation_status',
    }
    assert event['operation'] == 'tool_invocation'
    assert event['operation_status'] == outcome
    assert event['tool_name'] == (
        '<unknown>' if outcome == 'unknown_tool' else 'sample_tool'
    )
    assert event['service'] == service
    assert event['client_id'] == 'client-id'
    assert event['auth_policy'] == 'legacy_full'
    assert event['request_id'] == 'authenticated-request'
    assert event['elapsed_seconds'] > 0
    assert datetime.fromisoformat(event['timestamp']).tzinfo is not None
    assert 'private' not in lines[0] and '@example.com' not in lines[0]


@pytest.mark.parametrize(
    'outcome', ['success', 'tool_error', 'policy_denial', 'unknown_tool']
)
def test_real_http_tools_call_is_audited_once(
    service_config: ServiceConfig, outcome: str
) -> None:
    config = replace(
        service_config,
        dcr_default_policy='legacy_full',
        allowed_hosts=('mcp.example.test',),
    )

    class ProbeExtension(Extension):
        """Register synthetic HTTP operation."""

        def register_tools(self, registrar: ToolRegistrar) -> None:
            """Register audited test operation."""

            @registrar.tool(name='http_probe')
            def probe(value: str) -> str:
                """Return controlled test content."""
                if outcome == 'tool_error':
                    raise ValueError('secret response content')
                return 'secret response content'

    app, server, state = create_service_app(config, [ProbeExtension()])
    registered = state.register_client(
        ('https://client.example.test/callback',)
    )
    if outcome == 'policy_denial':
        state.set_client_policy(registered.client.client_id, MCP_READONLY_V1)
    issued = state.issue_access_token(
        client_id=registered.client.client_id,
        resource=config.resource_url,
    )
    name = 'private@example.com' if outcome == 'unknown_tool' else 'http_probe'
    with TestClient(app, base_url='https://mcp.example.test') as client:
        response = client.post(
            config.mcp_path,
            headers={
                'Authorization': f'Bearer {issued.access_token}',
                'Accept': 'application/json, text/event-stream',
            },
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {
                    'name': name,
                    'arguments': {'value': 'secret input'},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()['result'].get('isError', False) == (
            outcome != 'success'
        )
    records = [
        json.loads(line)
        for line in config.audit_log_path.read_text().splitlines()
    ]
    events = [r for r in records if r.get('operation') == 'tool_invocation']
    assert len(events) == 1
    assert events[0]['operation_status'] == outcome
    assert events[0]['client_id'] == registered.client.client_id
    assert len(events[0]['request_id']) == 32
    assert 'secret' not in json.dumps(
        events
    ) and '@example.com' not in json.dumps(events)


@pytest.mark.asyncio
async def test_audit_failure_preserves_result_without_replay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = PolicyMCPServer('gmail')
    writes: list[object] = []
    actions: list[str] = []

    def unavailable(record: dict[str, object]) -> None:
        """Reject synthetic audit append."""
        writes.append(record)
        raise AuditError('sensitive disk path and payload')

    server.set_tool_audit('gmail', unavailable)

    @ToolRegistrar(server).tool(name='sample_action')
    def sample_action() -> str:
        """Record one synthetic action."""
        actions.append('once')
        return 'original result'

    principal = AuthenticatedPrincipal(
        principal_id='principal',
        credential_id='credential',
        client_id='client-id',
        policy='legacy_full',
        capabilities=frozenset(),
        full_access=True,
    )
    token = set_request_context(principal, 'audit-failure-request')
    try:
        result = await server.call_tool('sample_action', {})
    finally:
        reset_request_context(token)
    assert result.content[0].text == 'original result'
    assert actions == ['once'] and len(writes) == 1
    assert 'tool_audit_write_failed' in caplog.text
    assert 'audit-failure-request' in caplog.text
    assert 'sensitive' not in caplog.text
