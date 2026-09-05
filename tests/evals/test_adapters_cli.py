"""Test live evaluation adapters and CLI."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from google_workspace_mcp.evals import adapters
from google_workspace_mcp.evals import cli as eval_cli
from google_workspace_mcp.evals.adapters import (
    DEEPSEEK_API_BASE_URL,
    DeepSeekModelGateway,
    LoopbackOAuthHandler,
    OAuthSessionFactory,
    PersistentOAuthClientProvider,
    _safe_oauth_logging,
)
from google_workspace_mcp.evals.apply import save_bindings
from google_workspace_mcp.evals.cli import (
    _binding_private_values,
    _parser,
    _require_complete_oauth,
    main,
)
from google_workspace_mcp.evals.models import FixtureBindings, ServiceName
from google_workspace_mcp.evals.runner import ToolSpec
from google_workspace_mcp.evals.validation import READONLY_TOOLS

# === Helpers ===


def _service_urls() -> dict[ServiceName, str]:
    """Build exact service URLs."""
    return {
        service: f'https://mcp.example.test/{service.value}/mcp'
        for service in ServiceName
    }


def _free_port() -> int:
    """Reserve one ephemeral port number."""
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return int(listener.getsockname()[1])


# === Model adapter ===


def test_model_gateway_requires_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)

    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY is required'):
        DeepSeekModelGateway()


def test_model_gateway_disables_automatic_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def build_client(**kwargs: object) -> object:
        """Capture model client configuration."""
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(adapters, 'AsyncAnthropic', build_client)

    DeepSeekModelGateway(api_key='synthetic-key')

    assert captured['max_retries'] == 0
    assert captured['base_url'] == DEEPSEEK_API_BASE_URL


@pytest.mark.asyncio
async def test_model_gateway_uses_real_sdk_wire_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Return synthetic DeepSeek responses."""
        requests.append(request)
        if request.url.path.endswith('/count_tokens'):
            return httpx2.Response(200, json={'input_tokens': 7})
        return httpx2.Response(
            200,
            json={
                'id': 'msg_synthetic',
                'type': 'message',
                'role': 'assistant',
                'model': 'deepseek-v4-pro',
                'content': [
                    {
                        'type': 'tool_use',
                        'id': 'call-1',
                        'name': 'drive_get_file',
                        'input': {'file_id': 'drive_ledger_file'},
                    },
                    {
                        'type': 'tool_use',
                        'id': 'call-2',
                        'name': 'drive_search_files',
                        'input': {'exact_name': 'Synthetic ledger'},
                    },
                ],
                'stop_reason': 'tool_use',
                'stop_sequence': None,
                'usage': {'input_tokens': 9, 'output_tokens': 12},
            },
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = AsyncAnthropic(
        api_key='synthetic-key',
        base_url='https://api.deepseek.test/anthropic',
        http_client=http_client,
        max_retries=0,
    )
    monkeypatch.setattr(adapters, 'AsyncAnthropic', lambda **kwargs: client)
    gateway = DeepSeekModelGateway(api_key='synthetic-key')
    tools = (
        ToolSpec('drive_get_file', 'Read file.', {'type': 'object'}),
        ToolSpec('drive_search_files', 'Search files.', {'type': 'object'}),
    )
    messages: list[dict[str, Any]] = [
        {'role': 'user', 'content': 'Read one fixture.'}
    ]

    try:
        count = await gateway.count_tokens(messages, tools)
        turn = await gateway.create_message(messages, tools, max_tokens=2048)
    finally:
        await gateway.close()

    assert count == 7
    assert turn.stop_reason == 'tool_use'
    assert [call.name for call in turn.tool_calls] == [
        'drive_get_file',
        'drive_search_files',
    ]
    assert [request.url.path for request in requests] == [
        '/anthropic/v1/messages/count_tokens',
        '/anthropic/v1/messages',
    ]
    for request in requests:
        body = json.loads(request.content)
        assert body['model'] == 'deepseek-v4-pro'
        assert body['reasoning'] == {'effort': 'none'}


def test_fixture_cli_imports_without_model_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, 'anthropic', None)
    monkeypatch.setitem(
        sys.modules,
        'google_workspace_mcp.evals.adapters',
        None,
    )

    reloaded = importlib.reload(eval_cli)

    assert callable(reloaded.main)


# === MCP adapter ===


def test_oauth_factory_requires_exact_https_resources(tmp_path: Path) -> None:
    urls = _service_urls()
    urls[ServiceName.GMAIL] = 'https://mcp.example.test/gmail'

    with pytest.raises(ValueError, match='MCP service URL is invalid'):
        OAuthSessionFactory(urls, tmp_path / 'oauth')


def test_oauth_logging_boundary_suppresses_raw_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = 'oauth-control-secret-93df'
    logger = logging.getLogger('mcp.client.auth.oauth2')

    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            with _safe_oauth_logging():
                logger.exception('OAuth flow error')

    assert secret not in caplog.text
    assert 'OAuth flow error' not in caplog.text


def test_oauth_callback_ports_are_unique() -> None:
    assert len(set(adapters.CALLBACK_PORTS.values())) == len(ServiceName)


@pytest.mark.asyncio
async def test_loopback_callback_returns_code_state_and_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    monkeypatch.setitem(adapters.CALLBACK_PORTS, ServiceName.GMAIL, port)
    monkeypatch.setattr(adapters.webbrowser, 'open', lambda url: bool(url))
    handler = LoopbackOAuthHandler(ServiceName.GMAIL)

    await handler.redirect('https://auth.example.test/authorize')
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(
        b'GET /callback?code=abc&state=state-1&'
        b'iss=https%3A%2F%2Fissuer.example '
        b'HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    result = await handler.callback()

    assert result.code == 'abc'
    assert result.state == 'state-1'
    assert result.iss == 'https://issuer.example'
    assert response.startswith(b'HTTP/1.1 200 OK')


@pytest.mark.asyncio
async def test_loopback_callback_rejection_fails_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    monkeypatch.setitem(adapters.CALLBACK_PORTS, ServiceName.GMAIL, port)
    monkeypatch.setattr(adapters.webbrowser, 'open', lambda url: bool(url))
    handler = LoopbackOAuthHandler(ServiceName.GMAIL)

    await handler.redirect('https://auth.example.test/authorize')
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(
        b'GET /callback?error=access_denied HTTP/1.1\r\n'
        b'Host: 127.0.0.1\r\n\r\n'
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()

    with pytest.raises(RuntimeError, match='OAuth callback was rejected'):
        await handler.callback()
    assert response.startswith(b'HTTP/1.1 400 Bad Request')


@pytest.mark.asyncio
async def test_persisted_expired_token_refreshes_without_browser(
    tmp_path: Path,
) -> None:
    storage = adapters.FileTokenStorage(tmp_path / 'oauth' / 'gmail.json')
    await storage.set_tokens(
        OAuthToken(
            access_token='expired-access',
            refresh_token='synthetic-refresh',
            expires_in=-1,
            scope='mcp_readonly_v1',
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id='synthetic-client',
            redirect_uris=['http://127.0.0.1:43131/callback'],
            scope='mcp_readonly_v1',
            issuer='https://mcp.example.test/gmail',
        )
    )
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Return one refresh and resource response."""
        requests.append(request)
        if request.url.path == '/gmail/oauth/token':
            return httpx2.Response(
                200,
                json={
                    'access_token': 'refreshed-access',
                    'token_type': 'Bearer',
                    'expires_in': 3600,
                },
            )
        return httpx2.Response(200, json={'ok': True})

    async def reject_browser(url: str) -> None:
        """Reject an unexpected browser flow."""
        raise AssertionError(url)

    async def reject_callback() -> AuthorizationCodeResult:
        """Reject an unexpected callback flow."""
        raise AssertionError

    provider = PersistentOAuthClientProvider(
        server_url='https://mcp.example.test/gmail/mcp',
        client_metadata=OAuthClientMetadata(
            client_name='Synthetic client',
            redirect_uris=[AnyUrl('http://127.0.0.1:43131/callback')],
            scope='mcp_readonly_v1',
        ),
        storage=storage,
        redirect_handler=reject_browser,
        callback_handler=reject_callback,
        allow_browser=False,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(handler),
    ) as client:
        response = await client.get('https://mcp.example.test/gmail/mcp')

    assert response.status_code == 200
    assert [request.url.path for request in requests] == [
        '/gmail/oauth/token',
        '/gmail/mcp',
    ]
    assert requests[1].headers['Authorization'] == 'Bearer refreshed-access'


@pytest.mark.asyncio
async def test_provider_rejects_mismatched_stored_issuer_before_network(
    tmp_path: Path,
) -> None:
    storage = adapters.FileTokenStorage(tmp_path / 'oauth' / 'gmail.json')
    await storage.set_tokens(
        OAuthToken(
            access_token='expired-access',
            refresh_token='synthetic-refresh',
            expires_in=-1,
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id='synthetic-client',
            issuer='https://other.example.test/gmail',
        )
    )
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Record an unexpected request."""
        requests.append(request)
        return httpx2.Response(500)

    async def reject_browser(url: str) -> None:
        """Reject an unexpected browser flow."""
        raise AssertionError(url)

    async def reject_callback() -> AuthorizationCodeResult:
        """Reject an unexpected callback flow."""
        raise AssertionError

    provider = PersistentOAuthClientProvider(
        server_url='https://mcp.example.test/gmail/mcp',
        client_metadata=OAuthClientMetadata(
            client_name='Synthetic client',
            redirect_uris=[AnyUrl('http://127.0.0.1:43131/callback')],
        ),
        storage=storage,
        redirect_handler=reject_browser,
        callback_handler=reject_callback,
        allow_browser=False,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(handler),
    ) as client:
        with pytest.raises(
            adapters.OAuthFlowError,
            match='stored OAuth issuer does not match service',
        ):
            await client.get('https://mcp.example.test/gmail/mcp')

    assert requests == []


@pytest.mark.asyncio
async def test_provider_disables_interactive_flow_for_run(
    tmp_path: Path,
) -> None:
    browser_calls: list[str] = []

    async def record_browser(url: str) -> None:
        """Record an unexpected browser flow."""
        browser_calls.append(url)

    async def reject_callback() -> AuthorizationCodeResult:
        """Reject an unexpected callback flow."""
        raise AssertionError

    provider = PersistentOAuthClientProvider(
        server_url='https://mcp.example.test/gmail/mcp',
        client_metadata=OAuthClientMetadata(
            client_name='Synthetic client',
            redirect_uris=[AnyUrl('http://127.0.0.1:43131/callback')],
        ),
        storage=adapters.FileTokenStorage(tmp_path / 'oauth' / 'gmail.json'),
        redirect_handler=record_browser,
        callback_handler=reject_callback,
        allow_browser=False,
    )

    with pytest.raises(
        adapters.OAuthFlowError,
        match='interactive OAuth is disabled',
    ):
        await provider._perform_authorization()

    assert browser_calls == []


# === CLI ===


def test_validate_command_reports_public_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    evals_dir = Path(__file__).parents[2] / 'evals'

    result = main(['validate', '--evals-dir', str(evals_dir)])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output == {'catalog_count': 5, 'pair_count': 50, 'status': 'valid'}


def test_cli_exposes_no_key_or_bearer_options() -> None:
    help_text = _parser().format_help()

    assert '--api-key' not in help_text
    assert '--bearer-token' not in help_text


def test_cli_private_scan_includes_numeric_sheet_ids(
    tmp_path: Path,
    applied_bindings: FixtureBindings,
) -> None:
    bindings_path = tmp_path / 'evals' / 'bindings.json'
    bindings_path.parent.mkdir(mode=0o700)
    save_bindings(bindings_path, applied_bindings)
    arguments = _parser().parse_args(
        [
            'validate',
            '--evals-dir',
            str(tmp_path),
            '--bindings',
            str(bindings_path),
        ]
    )

    values = _binding_private_values(arguments)

    assert '41001' in values


@pytest.mark.asyncio
async def test_run_requires_complete_oauth_before_live_client(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match='evaluation OAuth state is incomplete',
    ):
        await _require_complete_oauth(tmp_path / 'oauth')


@pytest.mark.asyncio
async def test_run_rejects_non_readonly_oauth_scope(tmp_path: Path) -> None:
    oauth_directory = tmp_path / 'oauth'
    for service in ServiceName:
        storage = adapters.FileTokenStorage(
            oauth_directory / f'{service.value}.json'
        )
        await storage.set_tokens(
            OAuthToken(
                access_token=f'{service.value}-access',
                refresh_token=f'{service.value}-refresh',
                expires_in=3600,
                scope=' '.join(sorted(READONLY_TOOLS[service])),
            )
        )
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id=f'{service.value}-client',
                issuer=f'https://mcp.example.test/{service.value}',
            )
        )
    gmail_storage = adapters.FileTokenStorage(oauth_directory / 'gmail.json')
    await gmail_storage.set_tokens(
        OAuthToken(
            access_token='gmail-access',
            refresh_token='gmail-refresh',
            expires_in=3600,
            scope='full_access',
        )
    )

    with pytest.raises(
        ValueError,
        match='evaluation OAuth state is incomplete',
    ):
        await _require_complete_oauth(oauth_directory)


def test_cli_sanitizes_failures(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(['validate', '--evals-dir', '/missing/private-secret-path'])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert captured.err == 'evaluation command failed\n'
