"""Adapt live model and MCP clients."""

from __future__ import annotations

import asyncio
import logging
import os
import webbrowser
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
)
from contextlib import asynccontextmanager, contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

import anthropic
import httpx2
from anthropic import AsyncAnthropic
from anthropic.types import Message, MessageParam, ToolParam
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientMetadata,
    OAuthMetadata,
)
from pydantic import AnyHttpUrl, AnyUrl

from .models import ServiceName
from .oauth_storage import FileTokenStorage
from .runner import (
    MODEL_NAME,
    SYSTEM_PROMPT,
    ModelTurn,
    ToolCall,
    ToolSession,
    ToolSpec,
)
from .validation import READONLY_SCOPE

# === Constants ===

CALLBACK_PORTS = {
    ServiceName.GMAIL: 43131,
    ServiceName.CALENDAR: 43132,
    ServiceName.DRIVE: 43133,
    ServiceName.SHEETS: 43134,
    ServiceName.DOCS: 43135,
}
OAUTH_CALLBACK_SECONDS = 600.0
MCP_READ_SECONDS = 180.0
TOKEN_ENDPOINT_AUTH_METHOD: Literal['client_secret_post'] = (
    'client_secret_post'
)
_OAUTH_LOGGER_NAME = 'mcp.client.auth.oauth2'
DEEPSEEK_API_BASE_URL = 'https://api.deepseek.com/anthropic'
DEEPSEEK_REASONING = {'reasoning': {'effort': 'none'}}


@contextmanager
def _safe_oauth_logging() -> Generator[None]:
    """Suppress raw OAuth SDK failures."""
    logger = logging.getLogger(_OAUTH_LOGGER_NAME)
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    disabled = logger.disabled
    logger.handlers = [logging.NullHandler()]
    logger.setLevel(logging.CRITICAL + 1)
    logger.propagate = False
    logger.disabled = False
    try:
        yield
    finally:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


class DeepSeekModelGateway:
    """Call DeepSeek through Anthropic format."""

    model_name = MODEL_NAME
    version = anthropic.__version__

    def __init__(self, api_key: str | None = None) -> None:
        """Configure the model client."""
        resolved_key = (
            api_key if api_key is not None else os.getenv('DEEPSEEK_API_KEY')
        )
        if not resolved_key:
            raise ValueError('DEEPSEEK_API_KEY is required')
        self._client = AsyncAnthropic(
            api_key=resolved_key,
            base_url=DEEPSEEK_API_BASE_URL,
            max_retries=0,
            timeout=MCP_READ_SECONDS,
        )

    @staticmethod
    def _tools(tools: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
        """Convert model tool definitions."""
        return [
            {
                'name': tool.name,
                'description': tool.description,
                'input_schema': tool.input_schema,
            }
            for tool in tools
        ]

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
    ) -> int:
        """Count one model input."""
        message_params = cast(Iterable[MessageParam], messages)
        if tools:
            result = await self._client.messages.count_tokens(
                model=self.model_name,
                system=SYSTEM_PROMPT,
                messages=message_params,
                tools=cast(Iterable[ToolParam], self._tools(tools)),
                extra_body=DEEPSEEK_REASONING,
            )
        else:
            result = await self._client.messages.count_tokens(
                model=self.model_name,
                system=SYSTEM_PROMPT,
                messages=message_params,
                extra_body=DEEPSEEK_REASONING,
            )
        return result.input_tokens

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
        *,
        max_tokens: int,
    ) -> ModelTurn:
        """Create one model response."""
        message_params = cast(Iterable[MessageParam], messages)
        if tools:
            response = await self._client.messages.create(
                model=self.model_name,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                messages=message_params,
                tools=cast(Iterable[ToolParam], self._tools(tools)),
                extra_body=DEEPSEEK_REASONING,
            )
        else:
            response = await self._client.messages.create(
                model=self.model_name,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                messages=message_params,
                extra_body=DEEPSEEK_REASONING,
            )
        message = cast(Message, response)
        if message.model != self.model_name:
            raise ValueError('model response identity is invalid')
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        assistant_content: list[dict[str, Any]] = []
        for block in message.content:
            dumped = block.model_dump(mode='json', exclude_none=True)
            assistant_content.append(dumped)
            if block.type == 'tool_use':
                tool_calls.append(
                    ToolCall(
                        call_id=block.id,
                        name=block.name,
                        arguments=dict(block.input),
                    )
                )
            elif block.type == 'text':
                text_parts.append(block.text)
        return ModelTurn(
            stop_reason=message.stop_reason,
            assistant_content=tuple(assistant_content),
            tool_calls=tuple(tool_calls),
            final_text=''.join(text_parts) if text_parts else None,
            output_tokens=message.usage.output_tokens,
        )

    async def close(self) -> None:
        """Close the model client."""
        await self._client.close()


class PersistentOAuthClientProvider(OAuthClientProvider):
    """Restore persisted token timing and service endpoints."""

    def __init__(
        self,
        server_url: str,
        client_metadata: OAuthClientMetadata,
        storage: FileTokenStorage,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
        *,
        allow_browser: bool,
    ) -> None:
        """Configure one persistent OAuth provider."""
        super().__init__(
            server_url=server_url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        self._persistent_storage = storage
        self._allow_browser = allow_browser

    async def _initialize(self) -> None:
        """Restore persisted provider state."""
        await super()._initialize()
        self.context.token_expiry_time = (
            await self._persistent_storage.get_token_expiry()
        )
        issuer = AnyHttpUrl(self.context.server_url.removesuffix('/mcp'))
        if self.context.client_info is not None and (
            not self.context.client_info.issuer
            or self.context.client_info.issuer.rstrip('/')
            != str(issuer).rstrip('/')
        ):
            raise OAuthFlowError('stored OAuth issuer does not match service')
        self.context.auth_server_url = str(issuer)
        self.context.oauth_metadata = OAuthMetadata(
            issuer=issuer,
            authorization_endpoint=AnyHttpUrl(f'{issuer}/oauth/authorize'),
            token_endpoint=AnyHttpUrl(f'{issuer}/oauth/token'),
            registration_endpoint=AnyHttpUrl(f'{issuer}/oauth/register'),
        )

    async def _perform_authorization(self) -> httpx2.Request:
        """Reject interactive OAuth outside authorization."""
        if not self._allow_browser:
            raise OAuthFlowError('interactive OAuth is disabled')
        return await super()._perform_authorization()


class LoopbackOAuthHandler:
    """Receive one OAuth browser callback."""

    def __init__(self, service: ServiceName) -> None:
        """Configure one fixed callback."""
        self._port = CALLBACK_PORTS[service]
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None

    @property
    def redirect_uri(self) -> str:
        """Return the loopback redirect URI."""
        return f'http://127.0.0.1:{self._port}/callback'

    async def redirect(self, authorization_url: str) -> None:
        """Open the authorization URL."""
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        try:
            self._server = await asyncio.start_server(
                self._handle_request,
                '127.0.0.1',
                self._port,
            )
        except OSError as error:
            raise RuntimeError(
                'OAuth callback listener is unavailable'
            ) from error
        opened = await asyncio.to_thread(webbrowser.open, authorization_url)
        if not opened:
            await self.close()
            raise RuntimeError('OAuth browser could not be opened')

    async def callback(self) -> AuthorizationCodeResult:
        """Wait for the authorization callback."""
        if self._result is None:
            raise RuntimeError('OAuth callback listener was not started')
        try:
            return await asyncio.wait_for(
                self._result,
                timeout=OAUTH_CALLBACK_SECONDS,
            )
        finally:
            await self.close()

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one loopback callback request."""
        try:
            request_line = await reader.readline()
            parts = request_line.decode('ascii', errors='strict').split(' ')
            if len(parts) != 3 or parts[0] != 'GET':
                raise ValueError
            parsed = urlsplit(parts[1])
            if parsed.path != '/callback':
                raise ValueError
            query = parse_qs(parsed.query, keep_blank_values=True)
            code = query.get('code', [''])[0]
            state = query.get('state', [None])[0]
            issuer = query.get('iss', [None])[0]
            if not code:
                raise ValueError
            result = AuthorizationCodeResult(
                code=code,
                state=state,
                iss=issuer,
            )
            if self._result is not None and not self._result.done():
                self._result.set_result(result)
            status = '200 OK'
            body = b'Authorization completed. Return to the terminal.'
        except Exception:
            status = '400 Bad Request'
            body = b'Authorization callback was rejected.'
            if self._result is not None and not self._result.done():
                self._result.set_exception(
                    RuntimeError('OAuth callback was rejected')
                )
        headers = (
            f'HTTP/1.1 {status}\r\n'
            'Content-Type: text/plain; charset=utf-8\r\n'
            f'Content-Length: {len(body)}\r\n'
            'Connection: close\r\n\r\n'
        ).encode('ascii')
        writer.write(headers + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def close(self) -> None:
        """Close the callback listener."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


class LiveToolSession:
    """Adapt one live MCP client."""

    def __init__(self, client: Client) -> None:
        """Store one connected client."""
        self._client = client

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        """List live MCP tools."""
        result = await self._client.list_tools(cache_mode='refresh')
        return tuple(
            ToolSpec(
                name=tool.name,
                description=tool.description or '',
                input_schema=dict(tool.input_schema),
            )
            for tool in result.tools
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> object:
        """Call one live MCP tool."""
        result = await self._client.call_tool(
            name,
            arguments,
            read_timeout_seconds=MCP_READ_SECONDS,
        )
        if result.is_error:
            raise RuntimeError('MCP tool returned an error')
        if result.structured_content is None:
            return None
        return result.structured_content


class OAuthSessionFactory:
    """Create isolated OAuth MCP sessions."""

    version = version('mcp')

    def __init__(
        self,
        service_urls: dict[ServiceName, str],
        oauth_directory: Path,
        *,
        allow_browser: bool = False,
    ) -> None:
        """Configure five service clients."""
        if set(service_urls) != set(ServiceName):
            raise ValueError('five MCP service URLs are required')
        for service, service_url in service_urls.items():
            parsed = urlsplit(service_url)
            expected_path = f'/{service.value}/mcp'
            if (
                parsed.scheme != 'https'
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != expected_path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError('MCP service URL is invalid')
        self._service_urls = dict(service_urls)
        self._oauth_directory = oauth_directory
        self._allow_browser = allow_browser

    @asynccontextmanager
    async def __call__(
        self,
        service: ServiceName,
    ) -> AsyncIterator[ToolSession]:
        """Open one fresh MCP session."""
        with _safe_oauth_logging():
            callback = LoopbackOAuthHandler(service)
            storage = FileTokenStorage(
                self._oauth_directory / f'{service.value}.json'
            )
            metadata = OAuthClientMetadata(
                client_name=f'Hawkx Stage 12 {service.value}',
                redirect_uris=[AnyUrl(callback.redirect_uri)],
                scope=READONLY_SCOPE,
                token_endpoint_auth_method=TOKEN_ENDPOINT_AUTH_METHOD,
            )
            provider = PersistentOAuthClientProvider(
                server_url=self._service_urls[service],
                client_metadata=metadata,
                storage=storage,
                redirect_handler=callback.redirect,
                callback_handler=callback.callback,
                allow_browser=self._allow_browser,
            )
            try:
                async with httpx2.AsyncClient(
                    auth=provider,
                    timeout=MCP_READ_SECONDS,
                    follow_redirects=False,
                ) as http_client:
                    transport = streamable_http_client(
                        self._service_urls[service],
                        http_client=http_client,
                        terminate_on_close=False,
                    )
                    async with Client(
                        transport,
                        read_timeout_seconds=MCP_READ_SECONDS,
                        cache=None,
                    ) as client:
                        yield LiveToolSession(client)
            finally:
                await callback.close()
