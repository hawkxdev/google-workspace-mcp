"""Call Docs provider methods."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import TypeAdapter, ValidationError

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)

from .constants import (
    MAX_DOCS_BATCH_OPERATIONS,
    MAX_DOCS_BLOCKS,
    MAX_DOCS_ID_CHARS,
    MAX_DOCS_OUTPUT_CHARS,
    MAX_DOCS_REPLACEMENTS,
    MAX_DOCS_TEXT_CHARS,
    MAX_DOCS_TITLE_CHARS,
    REQUEST_RETRIES,
)
from .errors import (
    DocsConflictError,
    DocsError,
    DocsInputError,
    DocsNotFoundError,
    DocsProviderError,
    DocsRateLimitError,
    DocsScopeError,
)
from .schemas import (
    DocsAlignment,
    DocsBatchOperation,
    DocsBatchOperationType,
    DocsBatchReply,
    DocsBatchResult,
    DocsBulletPreset,
    DocsContentResponse,
    DocsCreateResult,
    DocsMutationResult,
    DocsNamedStyle,
    DocsReplaceResult,
    DocumentSummary,
)
from .structure import (
    build_tab_segment,
    count_paragraph_matches,
    parse_document_tabs,
    project_tab_content,
    validate_content_range,
    validate_delete_range,
    validate_insert_index,
)

ServiceBuilder = Callable[[GoogleCredentials], Any]

_SUGGESTIONS_VIEW_MODE = 'PREVIEW_WITHOUT_SUGGESTIONS'
_UNAVAILABLE = 'Docs request is temporarily unavailable'

_SCOPE_REASONS = frozenset(
    {
        'insufficientPermissions',
        'insufficientFilePermissions',
        'insufficientScope',
        'ACCESS_TOKEN_SCOPE_INSUFFICIENT',
        'PERMISSION_DENIED',
    }
)

_RATE_LIMIT_REASONS = frozenset(
    {
        'rateLimitExceeded',
        'userRateLimitExceeded',
        'quotaExceeded',
        'RESOURCE_EXHAUSTED',
    }
)

_CONFLICT_REASONS = frozenset(
    {
        'conditionNotMet',
        'failedPrecondition',
        'FAILED_PRECONDITION',
    }
)


def build_docs_service(credentials: GoogleCredentials) -> Any:
    """Build Docs provider service."""
    return build(
        'docs',
        'v1',
        credentials=credentials.to_google_credentials(),
        cache_discovery=False,
        static_discovery=True,
    )


# Caller input guards


def _bounded_identifier(value: Any, label: str) -> str:
    """Validate bounded caller identifier."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or '\x00' in value
        or len(value) > MAX_DOCS_ID_CHARS
    ):
        raise DocsInputError(f'{label} is required and must be bounded')
    return value


def _validate_caps(max_blocks: int, max_chars: int) -> None:
    """Validate requested output caps."""
    if (
        not isinstance(max_blocks, int)
        or isinstance(max_blocks, bool)
        or not 1 <= max_blocks <= MAX_DOCS_BLOCKS
    ):
        raise DocsInputError(
            f'max_blocks must be between 1 and {MAX_DOCS_BLOCKS}'
        )
    if (
        not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or not 1 <= max_chars <= MAX_DOCS_OUTPUT_CHARS
    ):
        raise DocsInputError(
            f'max_chars must be between 1 and {MAX_DOCS_OUTPUT_CHARS}'
        )


def _validate_requested_range(
    start_index: int | None,
    end_index: int | None,
) -> None:
    """Validate requested projection range."""
    for value in (start_index, end_index):
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise DocsInputError('Docs range is invalid')
        if value < 0:
            raise DocsInputError('Docs range is invalid')
    if (
        start_index is not None
        and end_index is not None
        and start_index >= end_index
    ):
        raise DocsInputError('Docs range is invalid')


def _validate_index(value: Any) -> int:
    """Validate caller index value."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DocsInputError('Docs index is invalid')
    return value


def _validate_body_text(value: Any) -> str:
    """Validate bounded insertion text."""
    if (
        not isinstance(value, str)
        or not value
        or '\x00' in value
        or len(value) > MAX_DOCS_TEXT_CHARS
    ):
        raise DocsInputError(
            f'Docs text must be 1 to {MAX_DOCS_TEXT_CHARS} characters'
        )
    return value


def _validate_replacement_text(value: Any) -> str:
    """Validate bounded replacement text."""
    if (
        not isinstance(value, str)
        or '\x00' in value
        or len(value) > MAX_DOCS_TEXT_CHARS
    ):
        raise DocsInputError(
            f'Docs replacement text must be at most '
            f'{MAX_DOCS_TEXT_CHARS} characters'
        )
    return value


def _validate_search_literal(value: Any) -> str:
    """Validate single line literal."""
    if (
        not isinstance(value, str)
        or not value
        or '\x00' in value
        or '\n' in value
        or '\r' in value
        or len(value) > MAX_DOCS_TEXT_CHARS
    ):
        raise DocsInputError(
            'Docs search text must be one bounded line without newlines'
        )
    return value


def _validate_expected_occurrences(value: Any) -> int:
    """Validate expected replacement count."""
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_DOCS_REPLACEMENTS
    ):
        raise DocsInputError(
            f'expected_occurrences must be between 1 '
            f'and {MAX_DOCS_REPLACEMENTS}'
        )
    return value


def _validate_match_case(value: Any) -> bool:
    """Validate explicit match flag."""
    if not isinstance(value, bool):
        raise DocsInputError('match_case must be a boolean')
    return value


def _validate_title(value: Any) -> str:
    """Validate bounded document title."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or '\x00' in value
        or len(value) > MAX_DOCS_TITLE_CHARS
    ):
        raise DocsInputError(
            f'Docs title must be 1 to {MAX_DOCS_TITLE_CHARS} characters'
        )
    return value


_OPERATION_ADAPTER: TypeAdapter[Any] = TypeAdapter(DocsBatchOperation)

_INVALID_OPERATION = 'Docs batch operation is invalid'

_NAMED_STYLE_VALUES: dict[DocsNamedStyle, str] = {
    DocsNamedStyle.TITLE: 'TITLE',
    DocsNamedStyle.SUBTITLE: 'SUBTITLE',
    DocsNamedStyle.NORMAL_TEXT: 'NORMAL_TEXT',
    DocsNamedStyle.HEADING_1: 'HEADING_1',
    DocsNamedStyle.HEADING_2: 'HEADING_2',
    DocsNamedStyle.HEADING_3: 'HEADING_3',
    DocsNamedStyle.HEADING_4: 'HEADING_4',
    DocsNamedStyle.HEADING_5: 'HEADING_5',
    DocsNamedStyle.HEADING_6: 'HEADING_6',
}

_ALIGNMENT_VALUES: dict[DocsAlignment, str] = {
    DocsAlignment.START: 'START',
    DocsAlignment.CENTER: 'CENTER',
    DocsAlignment.END: 'END',
    DocsAlignment.JUSTIFIED: 'JUSTIFIED',
}

_BULLET_PRESETS: dict[DocsBulletPreset, str] = {
    DocsBulletPreset.DISC_CIRCLE_SQUARE: 'BULLET_DISC_CIRCLE_SQUARE',
    DocsBulletPreset.ARROW_DIAMOND_DISC: 'BULLET_ARROW_DIAMOND_DISC',
    DocsBulletPreset.CHECKBOX: 'BULLET_CHECKBOX',
    DocsBulletPreset.DECIMAL_ALPHA_ROMAN: 'NUMBERED_DECIMAL_ALPHA_ROMAN',
    DocsBulletPreset.DECIMAL_NESTED: 'NUMBERED_DECIMAL_NESTED',
}

_TEXT_STYLE_FIELDS = ('bold', 'italic', 'underline', 'strikethrough')


def _validate_operations(operations: Any) -> tuple[Any, ...]:
    """Validate typed batch operations."""
    if not isinstance(operations, Sequence) or isinstance(
        operations, str | bytes
    ):
        raise DocsInputError('Docs operations must be a bounded sequence')
    if not operations:
        raise DocsInputError('Docs batch requires at least one operation')
    if len(operations) > MAX_DOCS_BATCH_OPERATIONS:
        raise DocsInputError(
            f'Docs batch accepts at most {MAX_DOCS_BATCH_OPERATIONS} '
            'operations'
        )
    validated: list[Any] = []
    for item in operations:
        try:
            validated.append(_OPERATION_ADAPTER.validate_python(item))
        except ValidationError as error:
            raise DocsInputError(_INVALID_OPERATION) from error
    return tuple(validated)


def _text_style_body(operation: Any) -> dict[str, Any]:
    """Build text style body."""
    body = {
        field: getattr(operation, field)
        for field in _TEXT_STYLE_FIELDS
        if getattr(operation, field) is not None
    }
    if not body:
        raise DocsInputError(
            f'{_INVALID_OPERATION}: text style requires one allowed field'
        )
    return body


def _paragraph_style_body(operation: Any) -> dict[str, Any]:
    """Build paragraph style body."""
    body: dict[str, Any] = {}
    if operation.named_style is not None:
        body['namedStyleType'] = _NAMED_STYLE_VALUES[operation.named_style]
    if operation.alignment is not None:
        body['alignment'] = _ALIGNMENT_VALUES[operation.alignment]
    if not body:
        raise DocsInputError(
            f'{_INVALID_OPERATION}: paragraph style requires one value'
        )
    return body


def _validate_operation_payload(operation: Any) -> None:
    """Validate caller operation payload."""
    kind = operation.operation
    if kind is DocsBatchOperationType.INSERT_TEXT:
        _validate_body_text(operation.text)
        _validate_index(operation.index)
    elif kind is DocsBatchOperationType.INSERT_PAGE_BREAK:
        _validate_index(operation.index)
    elif kind is DocsBatchOperationType.REPLACE_TEXT:
        _validate_search_literal(operation.search_text)
        _validate_replacement_text(operation.replacement_text)
        _validate_expected_occurrences(operation.expected_occurrences)
    elif kind is DocsBatchOperationType.UPDATE_TEXT_STYLE:
        _text_style_body(operation)
        _validate_index(operation.start_index)
        _validate_index(operation.end_index)
    elif kind is DocsBatchOperationType.UPDATE_PARAGRAPH_STYLE:
        _paragraph_style_body(operation)
        _validate_index(operation.start_index)
        _validate_index(operation.end_index)
    else:
        _validate_index(operation.start_index)
        _validate_index(operation.end_index)


def _range_body(
    start_index: int, end_index: int, tab_id: str
) -> dict[str, Any]:
    """Build tab scoped range."""
    return {
        'startIndex': start_index,
        'endIndex': end_index,
        'tabId': tab_id,
    }


def _location_body(index: int, tab_id: str) -> dict[str, Any]:
    """Build tab scoped location."""
    return {'index': index, 'tabId': tab_id}


def _mapping(value: Any) -> Mapping[str, Any]:
    """Require Docs response mapping."""
    if not isinstance(value, Mapping):
        raise DocsProviderError('Docs returned an invalid response')
    return value


class DocsGateway:
    """Normalize Docs provider operations."""

    def __init__(
        self,
        store: GoogleCredentialStore,
        *,
        service_builder: ServiceBuilder = build_docs_service,
        num_retries: int = REQUEST_RETRIES,
    ) -> None:
        """Initialize Docs provider gateway."""
        self._store = store
        self._service_builder = service_builder
        self._num_retries = num_retries

    def service(self) -> Any:
        """Build authenticated Docs service."""
        try:
            credentials = self._store.refresh()
            return self._service_builder(credentials)
        except DocsError:
            raise
        except Exception:
            raise DocsProviderError(
                'Docs credentials are unavailable'
            ) from None

    @staticmethod
    def _http_reason(error: HttpError) -> str | None:
        """Read safe Docs reason."""
        try:
            raw_content = error.content
            if isinstance(raw_content, bytes):
                raw_content = raw_content.decode('utf-8')
            if isinstance(raw_content, str):
                content = json.loads(raw_content)
                payload = content.get('error', {})
                errors = payload.get('errors', [])
                if isinstance(errors, list) and errors:
                    reason = errors[0].get('reason')
                    if isinstance(reason, str):
                        return reason
                status = payload.get('status')
                return status if isinstance(status, str) else None
        except AttributeError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        return None

    def _translate_http_error(self, error: HttpError) -> Exception:
        """Translate provider HTTP error."""
        status = int(getattr(error.resp, 'status', 0))
        reason = self._http_reason(error)

        if status == 404:
            return DocsNotFoundError('Docs resource was not found')

        if status == 412 or reason in _CONFLICT_REASONS:
            return DocsConflictError('Docs document revision changed')

        if status == 429 or reason in _RATE_LIMIT_REASONS:
            return DocsRateLimitError('Docs is temporarily rate limited')

        if status == 403 and reason in _SCOPE_REASONS:
            return DocsScopeError(
                'Google authorization lacks required Docs permissions'
            )

        if status == 401:
            return DocsProviderError('Google authorization requires renewal')

        if status == 400:
            return DocsProviderError('Docs rejected the request')

        if status == 403:
            return DocsProviderError('Docs request was forbidden')

        return DocsProviderError(_UNAVAILABLE)

    def _execute_raw(self, request: Any, retries: int) -> Any:
        """Execute raw Docs request."""
        try:
            return request.execute(num_retries=retries)
        except HttpError as error:
            raise self._translate_http_error(error) from None
        except TransportError, TimeoutError, ConnectionError, OSError:
            raise DocsProviderError(_UNAVAILABLE) from None

    def _execute(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped Docs request."""
        return _mapping(self._execute_raw(request, self._num_retries))

    def _execute_write(self, request: Any) -> Mapping[str, Any]:
        """Execute mapped write request."""
        return _mapping(self._execute_raw(request, 0))

    def _fetch_document(self, document_id: str) -> Mapping[str, Any]:
        """Fetch complete document resource."""
        service = self.service()
        request = service.documents().get(
            documentId=document_id,
            includeTabsContent=True,
            suggestionsViewMode=_SUGGESTIONS_VIEW_MODE,
        )
        return self._execute(request)

    def get_document(self, document_id: str) -> DocumentSummary:
        """Retrieve document tab metadata."""
        validated = _bounded_identifier(document_id, 'Document ID')
        return parse_document_tabs(self._fetch_document(validated))

    def read_content(
        self,
        document_id: str,
        tab_id: str,
        *,
        start_index: int | None = None,
        end_index: int | None = None,
        max_blocks: int = MAX_DOCS_BLOCKS,
        max_chars: int = MAX_DOCS_OUTPUT_CHARS,
    ) -> DocsContentResponse:
        """Read bounded tab content."""
        validated_document = _bounded_identifier(document_id, 'Document ID')
        validated_tab = _bounded_identifier(tab_id, 'Tab ID')
        _validate_caps(max_blocks, max_chars)
        _validate_requested_range(start_index, end_index)
        payload = self._fetch_document(validated_document)
        return project_tab_content(
            payload,
            validated_tab,
            start_index=start_index,
            end_index=end_index,
            max_blocks=max_blocks,
            max_chars=max_chars,
        )

    # Mutations

    def _preflight_document(
        self,
        document_id: str,
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        """Fetch document at revision."""
        payload = self._fetch_document(document_id)
        current = payload.get('revisionId')
        if not isinstance(current, str) or not current:
            raise DocsProviderError('Docs returned an invalid response')
        if current != required_revision_id:
            raise DocsConflictError('Docs document revision changed')
        return payload

    def _batch_update(
        self,
        document_id: str,
        requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        """Send atomic provider batch."""
        service = self.service()
        request = service.documents().batchUpdate(
            documentId=document_id,
            body={
                'requests': list(requests),
                'writeControl': {'requiredRevisionId': required_revision_id},
            },
        )
        return self._execute_write(request)

    @staticmethod
    def _next_revision(data: Mapping[str, Any]) -> str:
        """Read next required revision."""
        control = data.get('writeControl')
        if isinstance(control, Mapping):
            value = control.get('requiredRevisionId')
            if isinstance(value, str) and value:
                return value
        raise DocsProviderError('Docs returned an invalid response')

    @staticmethod
    def _single_reply(data: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read single provider reply."""
        replies = data.get('replies')
        if not isinstance(replies, list) or len(replies) != 1:
            raise DocsProviderError('Docs returned an invalid response')
        reply = replies[0]
        if not isinstance(reply, Mapping):
            raise DocsProviderError('Docs returned an invalid response')
        return reply

    def create_document(self, title: str) -> DocsCreateResult:
        """Create new empty document."""
        validated = _validate_title(title)
        service = self.service()
        request = service.documents().create(body={'title': validated})
        data = self._execute_write(request)
        tabs = data.get('tabs')
        if isinstance(tabs, list) and tabs:
            summary = parse_document_tabs(data)
        else:
            document_id = data.get('documentId')
            if not isinstance(document_id, str) or not document_id:
                raise DocsProviderError('Docs returned an invalid response')
            summary = parse_document_tabs(self._fetch_document(document_id))
        return DocsCreateResult(
            document_id=summary.document_id,
            title=summary.title,
            tab_id=summary.tabs[0].tab_id,
            required_revision_id=summary.revision_id,
        )

    def insert_text(
        self,
        document_id: str,
        tab_id: str,
        index: int,
        text: str,
        *,
        required_revision_id: str,
    ) -> DocsMutationResult:
        """Insert text at index."""
        document = _bounded_identifier(document_id, 'Document ID')
        tab = _bounded_identifier(tab_id, 'Tab ID')
        revision = _bounded_identifier(required_revision_id, 'Revision ID')
        position = _validate_index(index)
        value = _validate_body_text(text)
        payload = self._preflight_document(document, revision)
        segment = build_tab_segment(payload, tab)
        validate_insert_index(segment, position)
        data = self._batch_update(
            document,
            (
                {
                    'insertText': {
                        'text': value,
                        'location': {'index': position, 'tabId': tab},
                    }
                },
            ),
            revision,
        )
        return DocsMutationResult(
            document_id=document,
            tab_id=tab,
            required_revision_id=self._next_revision(data),
        )

    def delete_range(
        self,
        document_id: str,
        tab_id: str,
        start_index: int,
        end_index: int,
        *,
        required_revision_id: str,
    ) -> DocsMutationResult:
        """Delete half open range."""
        document = _bounded_identifier(document_id, 'Document ID')
        tab = _bounded_identifier(tab_id, 'Tab ID')
        revision = _bounded_identifier(required_revision_id, 'Revision ID')
        start = _validate_index(start_index)
        end = _validate_index(end_index)
        payload = self._preflight_document(document, revision)
        segment = build_tab_segment(payload, tab)
        validate_delete_range(segment, start, end)
        data = self._batch_update(
            document,
            (
                {
                    'deleteContentRange': {
                        'range': {
                            'startIndex': start,
                            'endIndex': end,
                            'tabId': tab,
                        }
                    }
                },
            ),
            revision,
        )
        return DocsMutationResult(
            document_id=document,
            tab_id=tab,
            required_revision_id=self._next_revision(data),
        )

    def replace_text(
        self,
        document_id: str,
        tab_id: str,
        search_text: str,
        replacement_text: str,
        *,
        required_revision_id: str,
        match_case: bool,
        expected_occurrences: int,
    ) -> DocsReplaceResult:
        """Replace bounded literal occurrences."""
        document = _bounded_identifier(document_id, 'Document ID')
        tab = _bounded_identifier(tab_id, 'Tab ID')
        revision = _bounded_identifier(required_revision_id, 'Revision ID')
        literal = _validate_search_literal(search_text)
        replacement = _validate_replacement_text(replacement_text)
        case_sensitive = _validate_match_case(match_case)
        expected = _validate_expected_occurrences(expected_occurrences)
        payload = self._preflight_document(document, revision)
        found = count_paragraph_matches(payload, tab, literal, case_sensitive)
        if found != expected:
            raise DocsInputError(
                f'expected_occurrences does not match the {found} '
                'matches found in the selected tab'
            )
        data = self._batch_update(
            document,
            (
                {
                    'replaceAllText': {
                        'containsText': {
                            'text': literal,
                            'matchCase': case_sensitive,
                        },
                        'replaceText': replacement,
                        'tabsCriteria': {'tabIds': [tab]},
                    }
                },
            ),
            revision,
        )
        reply = self._single_reply(data)
        result = reply.get('replaceAllText')
        if not isinstance(result, Mapping):
            raise DocsProviderError('Docs returned an invalid response')
        changed = result.get('occurrencesChanged')
        if not isinstance(changed, int) or isinstance(changed, bool):
            raise DocsProviderError('Docs returned an invalid response')
        if changed < 0:
            raise DocsProviderError('Docs returned an invalid response')
        return DocsReplaceResult(
            document_id=document,
            tab_id=tab,
            occurrences_changed=changed,
            required_revision_id=self._next_revision(data),
        )

    def _build_batch_request(
        self,
        operation: Any,
        tab_id: str,
    ) -> Mapping[str, Any]:
        """Map operation to request."""
        kind = operation.operation
        if kind is DocsBatchOperationType.INSERT_TEXT:
            return {
                'insertText': {
                    'text': operation.text,
                    'location': _location_body(operation.index, tab_id),
                }
            }
        if kind is DocsBatchOperationType.INSERT_PAGE_BREAK:
            return {
                'insertPageBreak': {
                    'location': _location_body(operation.index, tab_id)
                }
            }
        if kind is DocsBatchOperationType.DELETE_RANGE:
            return {
                'deleteContentRange': {
                    'range': _range_body(
                        operation.start_index, operation.end_index, tab_id
                    )
                }
            }
        if kind is DocsBatchOperationType.REPLACE_TEXT:
            return {
                'replaceAllText': {
                    'containsText': {
                        'text': operation.search_text,
                        'matchCase': operation.match_case,
                    },
                    'replaceText': operation.replacement_text,
                    'tabsCriteria': {'tabIds': [tab_id]},
                }
            }
        if kind is DocsBatchOperationType.UPDATE_TEXT_STYLE:
            body = _text_style_body(operation)
            return {
                'updateTextStyle': {
                    'range': _range_body(
                        operation.start_index, operation.end_index, tab_id
                    ),
                    'textStyle': body,
                    'fields': ','.join(
                        field for field in _TEXT_STYLE_FIELDS if field in body
                    ),
                }
            }
        if kind is DocsBatchOperationType.UPDATE_PARAGRAPH_STYLE:
            body = _paragraph_style_body(operation)
            return {
                'updateParagraphStyle': {
                    'range': _range_body(
                        operation.start_index, operation.end_index, tab_id
                    ),
                    'paragraphStyle': body,
                    'fields': ','.join(
                        field
                        for field in ('namedStyleType', 'alignment')
                        if field in body
                    ),
                }
            }
        if kind is DocsBatchOperationType.CREATE_BULLETS:
            return {
                'createParagraphBullets': {
                    'range': _range_body(
                        operation.start_index, operation.end_index, tab_id
                    ),
                    'bulletPreset': _BULLET_PRESETS[operation.preset],
                }
            }
        return {
            'deleteParagraphBullets': {
                'range': _range_body(
                    operation.start_index, operation.end_index, tab_id
                )
            }
        }

    def _validate_operation_structure(
        self,
        operation: Any,
        segment: Any,
        payload: Mapping[str, Any],
        tab_id: str,
    ) -> None:
        """Validate operation against structure."""
        kind = operation.operation
        if kind in {
            DocsBatchOperationType.INSERT_TEXT,
            DocsBatchOperationType.INSERT_PAGE_BREAK,
        }:
            validate_insert_index(segment, operation.index)
        elif kind is DocsBatchOperationType.DELETE_RANGE:
            validate_delete_range(
                segment, operation.start_index, operation.end_index
            )
        elif kind is DocsBatchOperationType.REPLACE_TEXT:
            found = count_paragraph_matches(
                payload,
                tab_id,
                operation.search_text,
                operation.match_case,
            )
            if found != operation.expected_occurrences:
                raise DocsInputError(
                    f'expected_occurrences does not match the {found} '
                    'matches found in the selected tab'
                )
        else:
            validate_content_range(
                segment, operation.start_index, operation.end_index
            )

    def batch_update(
        self,
        document_id: str,
        tab_id: str,
        operations: Sequence[Any],
        *,
        required_revision_id: str,
    ) -> DocsBatchResult:
        """Apply atomic typed batch."""
        document = _bounded_identifier(document_id, 'Document ID')
        tab = _bounded_identifier(tab_id, 'Tab ID')
        revision = _bounded_identifier(required_revision_id, 'Revision ID')
        validated = _validate_operations(operations)
        for operation in validated:
            _validate_operation_payload(operation)
        payload = self._preflight_document(document, revision)
        segment = build_tab_segment(payload, tab)
        for operation in validated:
            self._validate_operation_structure(
                operation, segment, payload, tab
            )
        requests = [
            self._build_batch_request(operation, tab)
            for operation in validated
        ]
        data = self._batch_update(document, requests, revision)
        replies = data.get('replies')
        if not isinstance(replies, list) or len(replies) != len(validated):
            raise DocsProviderError('Docs returned an invalid response')
        normalized: list[DocsBatchReply] = []
        for operation, raw_reply in zip(validated, replies, strict=True):
            if not isinstance(raw_reply, Mapping):
                raise DocsProviderError('Docs returned an invalid response')
            changed: int | None = None
            if operation.operation is DocsBatchOperationType.REPLACE_TEXT:
                result = raw_reply.get('replaceAllText')
                if not isinstance(result, Mapping):
                    raise DocsProviderError(
                        'Docs returned an invalid response'
                    )
                value = result.get('occurrencesChanged')
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    raise DocsProviderError(
                        'Docs returned an invalid response'
                    )
                changed = value
            normalized.append(
                DocsBatchReply(
                    operation=operation.operation,
                    occurrences_changed=changed,
                )
            )
        return DocsBatchResult(
            document_id=document,
            tab_id=tab,
            operation_count=len(validated),
            required_revision_id=self._next_revision(data),
            replies=tuple(normalized),
        )
