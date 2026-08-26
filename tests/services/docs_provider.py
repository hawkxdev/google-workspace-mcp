"""Provide Docs provider fakes."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

import httplib2  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)

EMOJI = '\U0001f604'


# Provider document builders


def utf16_units(value: str) -> int:
    """Count units for tests."""
    return len(value.encode('utf-16-le')) // 2


def text_run(start: int, content: str) -> dict[str, Any]:
    """Build provider text run."""
    return {
        'startIndex': start,
        'endIndex': start + utf16_units(content),
        'textRun': {'content': content, 'textStyle': {}},
    }


def paragraph(
    start: int,
    content: str,
    named_style: str = 'NORMAL_TEXT',
    bullet: dict[str, Any] | None = None,
    tail: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build provider paragraph element."""
    elements = [text_run(start, content)] if content else []
    elements.extend(tail)
    end = elements[-1]['endIndex'] if elements else start
    body: dict[str, Any] = {
        'elements': elements,
        'paragraphStyle': {'namedStyleType': named_style},
    }
    if bullet is not None:
        body['bullet'] = bullet
    return {'startIndex': start, 'endIndex': end, 'paragraph': body}


def section_break(start: int) -> dict[str, Any]:
    """Build provider section break."""
    return {
        'startIndex': start,
        'endIndex': start + 1,
        'sectionBreak': {'sectionStyle': {}},
    }


def table_of_contents(start: int, end: int) -> dict[str, Any]:
    """Build provider contents table."""
    return {
        'startIndex': start,
        'endIndex': end,
        'tableOfContents': {'content': []},
    }


def table(start: int, cell_texts: tuple[str, ...]) -> dict[str, Any]:
    """Build provider row table."""
    cursor = start + 2
    cells = []
    for content in cell_texts:
        cell_start = cursor
        inner = paragraph(cell_start + 1, content + '\n')
        cursor = inner['endIndex'] + 1
        cells.append(
            {
                'startIndex': cell_start,
                'endIndex': cursor,
                'content': [inner],
            }
        )
    row_end = cursor
    return {
        'startIndex': start,
        'endIndex': row_end + 1,
        'table': {
            'rows': 1,
            'columns': len(cell_texts),
            'tableRows': [
                {
                    'startIndex': start + 1,
                    'endIndex': row_end,
                    'tableCells': cells,
                }
            ],
        },
    }


def tab(
    tab_id: str,
    content: list[dict[str, Any]],
    index: int = 0,
    parent_tab_id: str | None = None,
    nesting_level: int = 0,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build provider document tab."""
    properties: dict[str, Any] = {
        'tabId': tab_id,
        'title': f'Tab {tab_id}',
        'index': index,
        'nestingLevel': nesting_level,
    }
    if parent_tab_id is not None:
        properties['parentTabId'] = parent_tab_id
    node: dict[str, Any] = {
        'tabProperties': properties,
        'documentTab': {'body': {'content': content}},
    }
    if children:
        node['childTabs'] = children
    return node


def document(
    tabs: list[dict[str, Any]],
    document_id: str = 'document-1',
    revision_id: str = 'revision-1',
) -> dict[str, Any]:
    """Build provider document resource."""
    return {
        'documentId': document_id,
        'title': 'Test Document',
        'revisionId': revision_id,
        'tabs': tabs,
    }


def simple_body() -> list[dict[str, Any]]:
    """Build simple provider body."""
    return [section_break(0), paragraph(1, 'Hello\n'), paragraph(7, 'World\n')]


def simple_document(
    document_id: str = 'document-1',
    revision_id: str = 'revision-1',
) -> dict[str, Any]:
    """Build single tab document."""
    return document(
        [tab('tab-1', simple_body())],
        document_id=document_id,
        revision_id=revision_id,
    )


def document_with_nested_tabs() -> dict[str, Any]:
    """Build nested tab document."""
    leaf = tab(
        'tab-3-1-1',
        simple_body(),
        index=0,
        parent_tab_id='tab-3-1',
        nesting_level=2,
    )
    middle = tab(
        'tab-3-1',
        simple_body(),
        index=0,
        parent_tab_id='tab-3',
        nesting_level=1,
        children=[leaf],
    )
    root = tab('tab-3', simple_body(), index=0, children=[middle])
    return document([root])


def batch_result(
    revision_id: str = 'revision-2',
    replies: tuple[dict[str, Any], ...] = (),
    document_id: str = 'document-1',
) -> dict[str, Any]:
    """Build provider batch response."""
    return {
        'documentId': document_id,
        'replies': list(replies),
        'writeControl': {'requiredRevisionId': revision_id},
    }


def replace_reply(occurrences: int) -> dict[str, Any]:
    """Build provider replace reply."""
    return {'replaceAllText': {'occurrencesChanged': occurrences}}


def created_document(
    document_id: str = 'document-9',
    title: str = 'New Document',
    revision_id: str = 'revision-1',
    with_tabs: bool = True,
) -> dict[str, Any]:
    """Build provider create response."""
    payload: dict[str, Any] = {
        'documentId': document_id,
        'title': title,
        'revisionId': revision_id,
    }
    if with_tabs:
        payload['tabs'] = [tab('tab-1', [paragraph(1, '\n')])]
    return payload


def deep_tab_document(depth: int) -> dict[str, Any]:
    """Build deeply nested tabs."""
    node = tab('deep', simple_body())
    for level in range(depth):
        node = tab(f'tab-{level}', simple_body(), children=[node])
    return document([node])


def nested_table_document(depth: int) -> dict[str, Any]:
    """Build deeply nested tables."""
    node: dict[str, Any] = paragraph(1, 'Hi')
    for _ in range(depth):
        node = {
            'startIndex': 1,
            'endIndex': 3,
            'table': {
                'rows': 1,
                'columns': 1,
                'tableRows': [
                    {
                        'startIndex': 1,
                        'endIndex': 3,
                        'tableCells': [
                            {
                                'startIndex': 1,
                                'endIndex': 3,
                                'content': [node],
                            }
                        ],
                    }
                ],
            },
        }
    return document([tab('tab-1', [node])])


# Provider service fakes


class FakeRequest:
    """Record provider request execution."""

    def __init__(
        self, value: Any = None, error: Exception | None = None
    ) -> None:
        """Initialize fake provider request."""
        self.value = value
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0) -> Any:
        """Execute configured provider response."""
        self.retries.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.value


class FakeDocumentsEndpoint:
    """Record documents endpoint calls."""

    def __init__(self) -> None:
        """Initialize documents endpoint fake."""
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []

    def queue(self, method: str, *values: Any) -> None:
        """Queue documents endpoint values."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Record documents endpoint request."""
        if not self.responses[method]:
            raise AssertionError(f'No queued response for {method}({kwargs})')
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def get(self, **kwargs: Any) -> FakeRequest:
        """Record documents get call."""
        request = self._call('get', kwargs)
        if kwargs.get('includeTabsContent') is not True and isinstance(
            request.value, dict
        ):
            stripped = {
                key: value
                for key, value in request.value.items()
                if key != 'tabs'
            }
            stripped['body'] = {'content': []}
            request.value = stripped
        return request

    def create(self, **kwargs: Any) -> FakeRequest:
        """Record documents create call."""
        return self._call('create', kwargs)

    def batchUpdate(self, **kwargs: Any) -> FakeRequest:
        """Record documents batchUpdate call."""
        return self._call('batchUpdate', kwargs)


class FakeDocsService:
    """Expose fake Docs endpoints."""

    def __init__(self) -> None:
        """Initialize Docs service fake."""
        self.documents_endpoint = FakeDocumentsEndpoint()

    def documents(self) -> FakeDocumentsEndpoint:
        """Return documents endpoint."""
        return self.documents_endpoint

    def queue(self, method: str, *values: Any) -> None:
        """Queue documents endpoint values."""
        self.documents_endpoint.queue(method, *values)

    def calls_for(self, method: str) -> list[dict[str, Any]]:
        """List recorded call arguments."""
        return [
            kwargs
            for name, kwargs, _ in self.documents_endpoint.calls
            if name == method
        ]

    @property
    def last_get(self) -> dict[str, Any] | None:
        """Return last get arguments."""
        calls = self.calls_for('get')
        return calls[-1] if calls else None

    @property
    def last_batch_update(self) -> dict[str, Any] | None:
        """Return last batchUpdate arguments."""
        calls = self.calls_for('batchUpdate')
        return calls[-1] if calls else None

    @property
    def last_write_control(self) -> dict[str, Any] | None:
        """Return last write control."""
        call = self.last_batch_update
        if call is None:
            return None
        body = call.get('body') or {}
        control = body.get('writeControl')
        return control if isinstance(control, dict) else None

    @property
    def last_requests(self) -> list[dict[str, Any]]:
        """Return last batch requests."""
        call = self.last_batch_update
        if call is None:
            return []
        body = call.get('body') or {}
        requests = body.get('requests')
        return list(requests) if isinstance(requests, list) else []


class FakeDocsStore(GoogleCredentialStore):
    """Return Docs test credentials."""

    def __init__(self) -> None:
        """Initialize Docs store fake."""
        self.calls = 0
        self.credentials = GoogleCredentials(
            token='test-token',
            refresh_token='test-refresh',
            client_id='test-client',
            client_secret='test-secret',
            scopes=('https://www.googleapis.com/auth/documents',),
        )

    def refresh(self, request: Any = None) -> GoogleCredentials:
        """Record credential refresh."""
        self.calls += 1
        return self.credentials


def make_http_error(status: int, reason: str | None = None) -> HttpError:
    """Build provider HTTP error."""
    response = httplib2.Response({'status': str(status)})
    payload: dict[str, Any] = {'error': {'code': status}}
    if reason:
        payload['error']['errors'] = [{'reason': reason}]
        payload['error']['status'] = reason
    content = json.dumps(payload).encode('utf-8')
    return HttpError(
        response,
        content,
        uri='https://docs.googleapis.com/v1/documents/secret123',
    )
