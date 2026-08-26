"""Test Docs provider gateway."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from google.auth.exceptions import TransportError

from google_workspace_mcp.google_auth import GoogleCredentials
from google_workspace_mcp.services.docs.client import (
    DocsGateway,
    build_docs_service,
)
from google_workspace_mcp.services.docs.constants import REQUEST_RETRIES
from google_workspace_mcp.services.docs.errors import (
    DocsConflictError,
    DocsInputError,
    DocsNotFoundError,
    DocsProviderError,
    DocsRateLimitError,
    DocsScopeError,
)
from tests.services.docs_provider import (
    FakeDocsService,
    FakeDocsStore,
    FakeRequest,
    document,
    document_with_nested_tabs,
    make_http_error,
    paragraph,
    simple_body,
    simple_document,
    tab,
)


@pytest.fixture
def fake_service() -> FakeDocsService:
    """Create fake Docs service."""
    return FakeDocsService()


@pytest.fixture
def store() -> FakeDocsStore:
    """Create fake credential store."""
    return FakeDocsStore()


@pytest.fixture
def gateway(
    store: FakeDocsStore, fake_service: FakeDocsService
) -> DocsGateway:
    """Create gateway under test."""
    return DocsGateway(store, service_builder=lambda _: fake_service)


def queue_error(fake_service: FakeDocsService, error: Exception) -> None:
    """Queue failing provider request."""
    fake_service.queue('get', FakeRequest(error=error))


def test_build_docs_service_creates_static_discovery_service() -> None:
    credentials = GoogleCredentials(
        token='test-token',
        refresh_token='test-refresh',
        client_id='test-client',
        client_secret='test-secret',
        scopes=('https://www.googleapis.com/auth/documents',),
    )
    with patch(
        'google_workspace_mcp.services.docs.client.build'
    ) as mock_build:
        mock_build.return_value = 'mock-service'
        service = build_docs_service(credentials)
        assert service == 'mock-service'
        args, kwargs = mock_build.call_args
        assert args == ('docs', 'v1')
        assert kwargs['cache_discovery'] is False
        assert kwargs['static_discovery'] is True
        assert kwargs['credentials'].token == 'test-token'


def test_gateway_service_builds_authenticated_service(
    store: FakeDocsStore, fake_service: FakeDocsService
) -> None:
    gateway = DocsGateway(store, service_builder=lambda _: fake_service)
    assert gateway.service() is fake_service
    assert store.calls == 1


def test_gateway_service_maps_credential_errors_safely(
    store: FakeDocsStore,
) -> None:
    def explode(_: Any) -> Any:
        """Raise credential build failure."""
        raise RuntimeError('/home/owner/token.json is unreadable')

    gateway = DocsGateway(store, service_builder=explode)
    with pytest.raises(DocsProviderError) as caught:
        gateway.service()
    assert 'token.json' not in str(caught.value)


def test_get_always_includes_tabs(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    gateway.get_document('document-1')
    assert fake_service.last_get == {
        'documentId': 'document-1',
        'includeTabsContent': True,
        'suggestionsViewMode': 'PREVIEW_WITHOUT_SUGGESTIONS',
    }


def test_get_document_uses_configured_retries(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    gateway.get_document('document-1')
    request = fake_service.documents_endpoint.calls[0][2]
    assert request.retries == [REQUEST_RETRIES]


def test_get_document_returns_recursive_tab_metadata(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', document_with_nested_tabs())
    summary = gateway.get_document('document-1')
    assert summary.document_id == 'document-1'
    assert summary.revision_id == 'revision-1'
    assert summary.tabs[0].children[0].children[0].tab_id == 'tab-3-1-1'


@pytest.mark.parametrize('document_id', ['', '   ', 'a\x00b'])
def test_get_document_rejects_invalid_identifier(
    gateway: DocsGateway, document_id: str
) -> None:
    with pytest.raises(DocsInputError, match='Document ID'):
        gateway.get_document(document_id)


def test_get_document_rejects_oversized_identifier(
    gateway: DocsGateway,
) -> None:
    with pytest.raises(DocsInputError, match='Document ID'):
        gateway.get_document('d' * 400)


def test_invalid_identifier_never_reaches_provider(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    with pytest.raises(DocsInputError):
        gateway.get_document('')
    assert fake_service.documents_endpoint.calls == []


def test_missing_revision_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    payload = simple_document()
    del payload['revisionId']
    fake_service.queue('get', payload)
    with pytest.raises(DocsProviderError, match='Docs response is invalid'):
        gateway.get_document('document-1')


def test_invalid_provider_mapping_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', ['not', 'a', 'mapping'])
    with pytest.raises(DocsProviderError):
        gateway.get_document('document-1')


def test_oversized_structure_is_rejected(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    nodes = [
        tab(f'tab-{number}', simple_body(), index=number)
        for number in range(300)
    ]
    fake_service.queue('get', document(nodes))
    with pytest.raises(DocsProviderError, match='Docs response is invalid'):
        gateway.get_document('document-1')


def test_not_found_maps_to_docs_not_found(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    queue_error(fake_service, make_http_error(404))
    with pytest.raises(DocsNotFoundError):
        gateway.get_document('document-1')


@pytest.mark.parametrize(
    'reason',
    [
        'insufficientPermissions',
        'insufficientFilePermissions',
        'ACCESS_TOKEN_SCOPE_INSUFFICIENT',
    ],
)
def test_insufficient_scope_maps_to_docs_scope_error(
    fake_service: FakeDocsService, gateway: DocsGateway, reason: str
) -> None:
    queue_error(fake_service, make_http_error(403, reason))
    with pytest.raises(DocsScopeError):
        gateway.get_document('document-1')


def test_plain_rate_limit_status_maps_to_rate_limit(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    queue_error(fake_service, make_http_error(429))
    with pytest.raises(DocsRateLimitError):
        gateway.get_document('document-1')


@pytest.mark.parametrize(
    'reason',
    ['rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded'],
)
def test_rate_limit_reasons_map_to_rate_limit(
    fake_service: FakeDocsService, gateway: DocsGateway, reason: str
) -> None:
    queue_error(fake_service, make_http_error(403, reason))
    with pytest.raises(DocsRateLimitError):
        gateway.get_document('document-1')


@pytest.mark.parametrize('status', [400, 412])
@pytest.mark.parametrize('reason', ['conditionNotMet', 'FAILED_PRECONDITION'])
def test_stale_revision_maps_to_conflict(
    fake_service: FakeDocsService,
    gateway: DocsGateway,
    status: int,
    reason: str,
) -> None:
    queue_error(fake_service, make_http_error(status, reason))
    with pytest.raises(DocsConflictError, match='revision'):
        gateway.get_document('document-1')


def test_precondition_status_alone_maps_to_conflict(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    queue_error(fake_service, make_http_error(412))
    with pytest.raises(DocsConflictError, match='revision'):
        gateway.get_document('document-1')


def test_forbidden_without_known_reason_maps_to_provider_error(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    queue_error(fake_service, make_http_error(403, 'somethingElse'))
    with pytest.raises(DocsProviderError):
        gateway.get_document('document-1')


def test_server_failure_maps_to_provider_error(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    queue_error(fake_service, make_http_error(503))
    with pytest.raises(DocsProviderError):
        gateway.get_document('document-1')


@pytest.mark.parametrize(
    'error',
    [
        TransportError('socket closed to docs.googleapis.com'),
        TimeoutError('timed out'),
        ConnectionError('connection reset'),
        OSError('host unreachable'),
    ],
)
def test_transport_failures_map_to_provider_error(
    fake_service: FakeDocsService, gateway: DocsGateway, error: Exception
) -> None:
    queue_error(fake_service, error)
    with pytest.raises(DocsProviderError) as caught:
        gateway.get_document('document-1')
    assert 'googleapis.com' not in str(caught.value)


@pytest.mark.parametrize('status', [400, 403, 404, 429, 500])
def test_provider_errors_never_leak_uri_or_payload(
    fake_service: FakeDocsService, gateway: DocsGateway, status: int
) -> None:
    queue_error(fake_service, make_http_error(status, 'someReason'))
    with pytest.raises(Exception) as caught:
        gateway.get_document('document-1')
    message = str(caught.value)
    assert 'secret123' not in message
    assert 'docs.googleapis.com' not in message
    assert 'someReason' not in message


def test_read_content_reuses_the_same_provider_request(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    gateway.read_content('document-1', 'tab-1')
    assert fake_service.last_get == {
        'documentId': 'document-1',
        'includeTabsContent': True,
        'suggestionsViewMode': 'PREVIEW_WITHOUT_SUGGESTIONS',
    }


def test_read_content_rejects_unknown_tab(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    fake_service.queue('get', simple_document())
    with pytest.raises(DocsNotFoundError, match='Docs tab was not found'):
        gateway.read_content('document-1', 'tab-missing')


def test_legacy_body_is_never_read_by_gateway(
    fake_service: FakeDocsService, gateway: DocsGateway
) -> None:
    payload = simple_document()
    payload['body'] = {'content': [paragraph(1, 'Legacy body\n')]}
    fake_service.queue('get', payload)
    content = gateway.read_content('document-1', 'tab-1')
    rendered = ''.join(
        element.content or ''
        for block in content.blocks
        if getattr(block, 'elements', None)
        for element in block.elements
    )
    assert 'Legacy body' not in rendered
