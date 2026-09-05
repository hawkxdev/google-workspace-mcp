"""Validate evaluation XML catalogs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .catalog import EXPECTED_LOGICAL_REFS, OBJECTS_BY_REF
from .models import FIXTURE_VERSION, ServiceName
from .normalizers import (
    AnswerNormalizationError,
    NormalizerName,
    normalize_answer,
)

# === Constants ===

SERVICE_ORDER = (
    ServiceName.GMAIL,
    ServiceName.CALENDAR,
    ServiceName.DRIVE,
    ServiceName.SHEETS,
    ServiceName.DOCS,
)
PAIR_ELEMENTS = (
    'task_id',
    'question',
    'expected_answer',
    'normalizer',
    'fixture_refs',
    'allowed_tools',
    'minimum_mcp_calls',
)
READONLY_TOOLS: dict[ServiceName, frozenset[str]] = {
    ServiceName.GMAIL: frozenset(
        {
            'gmail_search_messages',
            'gmail_search_threads',
            'gmail_get_message',
            'gmail_get_thread',
            'gmail_list_labels',
            'gmail_list_drafts',
            'gmail_get_draft',
        }
    ),
    ServiceName.CALENDAR: frozenset(
        {
            'calendar_list_calendars',
            'calendar_search_events',
            'calendar_get_event',
            'calendar_list_event_instances',
            'calendar_get_freebusy',
        }
    ),
    ServiceName.DRIVE: frozenset(
        {'drive_search_files', 'drive_get_file', 'drive_list_folder'}
    ),
    ServiceName.SHEETS: frozenset(
        {
            'sheets_get_spreadsheet',
            'sheets_read_range',
            'sheets_batch_read_ranges',
        }
    ),
    ServiceName.DOCS: frozenset({'docs_get_document', 'docs_read_content'}),
}
MAX_XML_BYTES = 256 * 1024
READONLY_SCOPE = 'mcp_readonly_v1'
_PRIVATE_PATTERNS = (
    re.compile(r'(?i)\b(?:ya29\.|bearer\s+)[A-Za-z0-9._~-]+'),
    re.compile(r'\b1//[A-Za-z0-9_-]+\b'),
    re.compile(r'\bGOCSPX-[A-Za-z0-9_-]+\b'),
    re.compile(r'\bAIza[A-Za-z0-9_-]{20,}\b'),
    re.compile(r'\bsk-ant-[A-Za-z0-9_-]+\b'),
    re.compile(r'\b(?:v1|r1)\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'),
    re.compile(r'(?i)\bclient_secret\b'),
    re.compile(r'(?i)(?:^|[^A-Za-z0-9_])private[\\/]'),
    re.compile(r'(?i)(?:^|[^A-Za-z0-9_])google-tokens[\\/]'),
    re.compile(r'(?i)(?:^|[^A-Za-z0-9_])oauth[\\/]'),
    re.compile(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b'),
)


class CatalogValidationError(ValueError):
    """Report invalid evaluation catalogs."""


@dataclass(frozen=True, slots=True)
class EvaluationPair:
    """Store one validated pair."""

    task_id: str
    question: str
    expected_answer: str
    normalizer: NormalizerName
    fixture_refs: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    minimum_mcp_calls: int


@dataclass(frozen=True, slots=True)
class EvaluationCatalog:
    """Store one service catalog."""

    fixture_version: str
    service: ServiceName
    pairs: tuple[EvaluationPair, ...]


def _text(element: ET.Element, name: str, task_id: str) -> str:
    """Read one scalar element."""
    children = list(element)
    value = (element.text or '').strip()
    if children or not value or element.attrib:
        raise CatalogValidationError(f'{task_id} has invalid {name}')
    return value


def _items(
    element: ET.Element,
    item_name: str,
    field_name: str,
    task_id: str,
) -> tuple[str, ...]:
    """Read one nested item collection."""
    if element.attrib or (element.text or '').strip():
        raise CatalogValidationError(f'{task_id} has invalid {field_name}')
    items = tuple(_text(item, item_name, task_id) for item in list(element))
    if not items or any(
        item.tag != item_name or (item.tail or '').strip() for item in element
    ):
        raise CatalogValidationError(f'{task_id} has invalid {field_name}')
    if len(items) != len(set(items)):
        raise CatalogValidationError(f'{task_id} has duplicate {field_name}')
    return items


def _reject_private_values(
    public_text: str,
    forbidden_values: tuple[str, ...],
) -> None:
    """Reject private catalog content."""
    if any(
        value and value in public_text for value in forbidden_values
    ) or any(pattern.search(public_text) for pattern in _PRIVATE_PATTERNS):
        raise CatalogValidationError('catalog contains a private value')


def _parse_xml(path: Path, forbidden_values: tuple[str, ...]) -> ET.Element:
    """Parse one bounded XML file."""
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise CatalogValidationError('catalog file is unavailable') from error
    if not payload or len(payload) > MAX_XML_BYTES:
        raise CatalogValidationError('catalog file size is invalid')
    public_text = payload.decode('utf-8')
    if '<!DOCTYPE' in public_text or '<!ENTITY' in public_text:
        raise CatalogValidationError(
            'catalog contains an unsafe XML declaration'
        )
    _reject_private_values(public_text, forbidden_values)
    try:
        root = ET.fromstring(payload)  # noqa: S314
    except ET.ParseError as error:
        raise CatalogValidationError('catalog contains invalid XML') from error
    _reject_private_values(
        ET.tostring(root, encoding='unicode'),
        forbidden_values,
    )
    return root


def _parse_pair(
    pair: ET.Element,
    service: ServiceName,
) -> EvaluationPair:
    """Parse one evaluation pair."""
    task_hint = pair.findtext('task_id') or service.value
    if (
        pair.attrib
        or (pair.text or '').strip()
        or any((child.tail or '').strip() for child in pair)
        or tuple(child.tag for child in pair) != PAIR_ELEMENTS
    ):
        raise CatalogValidationError(f'{task_hint} has unexpected elements')
    task_id = _text(pair[0], 'task_id', task_hint)
    question = _text(pair[1], 'question', task_id)
    expected_answer = _text(pair[2], 'expected_answer', task_id)
    normalizer_text = _text(pair[3], 'normalizer', task_id)
    fixture_refs = _items(pair[4], 'ref', 'fixture refs', task_id)
    allowed_tools = _items(pair[5], 'tool', 'allowed tools', task_id)
    minimum_calls = _text(pair[6], 'minimum_mcp_calls', task_id)
    try:
        normalizer = NormalizerName(normalizer_text)
        enum_values = (
            (expected_answer,) if normalizer is NormalizerName.ENUM else None
        )
        canonical_expected = normalize_answer(
            expected_answer,
            normalizer,
            enum_values=enum_values,
        )
    except (ValueError, AnswerNormalizationError) as error:
        raise CatalogValidationError(
            f'{task_id} has an invalid expected answer'
        ) from error
    if canonical_expected != expected_answer:
        raise CatalogValidationError(
            f'{task_id} expected answer is not canonical'
        )
    if any(
        logical_ref not in EXPECTED_LOGICAL_REFS
        for logical_ref in fixture_refs
    ):
        raise CatalogValidationError(
            f'{task_id} contains an unknown fixture ref'
        )
    if any(
        OBJECTS_BY_REF[logical_ref].service is not service
        for logical_ref in fixture_refs
    ):
        raise CatalogValidationError(
            f'{task_id} contains a foreign fixture ref'
        )
    if not set(allowed_tools) <= READONLY_TOOLS[service]:
        raise CatalogValidationError(f'{task_id} contains a forbidden tool')
    try:
        parsed_minimum_calls = int(minimum_calls)
    except ValueError as error:
        raise CatalogValidationError(
            f'{task_id} has invalid minimum_mcp_calls'
        ) from error
    if not len(allowed_tools) <= parsed_minimum_calls <= 12:
        raise CatalogValidationError(
            f'{task_id} has invalid minimum_mcp_calls'
        )
    return EvaluationPair(
        task_id=task_id,
        question=question,
        expected_answer=expected_answer,
        normalizer=normalizer,
        fixture_refs=fixture_refs,
        allowed_tools=allowed_tools,
        minimum_mcp_calls=parsed_minimum_calls,
    )


def _parse_catalog(
    path: Path,
    service: ServiceName,
    forbidden_values: tuple[str, ...],
) -> EvaluationCatalog:
    """Parse one service catalog."""
    root = _parse_xml(path, forbidden_values)
    expected_attributes = {
        'fixture_version': FIXTURE_VERSION,
        'service': service.value,
    }
    if (
        root.tag != 'evaluation'
        or root.attrib != expected_attributes
        or (root.text or '').strip()
        or any((child.tail or '').strip() for child in root)
    ):
        raise CatalogValidationError(
            f'{service.value} catalog root is invalid'
        )
    pairs = tuple(_parse_pair(pair, service) for pair in list(root))
    if any(pair.tag != 'qa_pair' for pair in root) or len(pairs) != 10:
        raise CatalogValidationError(
            f'{service.value} catalog must contain exactly ten '
            'qa_pair elements'
        )
    expected_ids = tuple(
        f'{service.value}_{index:02d}' for index in range(1, 11)
    )
    if tuple(pair.task_id for pair in pairs) != expected_ids:
        raise CatalogValidationError(f'{service.value} task ids are invalid')
    return EvaluationCatalog(
        fixture_version=FIXTURE_VERSION,
        service=service,
        pairs=pairs,
    )


def load_evaluation_catalogs(
    directory: Path,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> tuple[EvaluationCatalog, ...]:
    """Load and validate five catalogs."""
    expected_paths = {
        directory / f'{service.value}.xml' for service in SERVICE_ORDER
    }
    actual_paths = set(directory.glob('*.xml'))
    if actual_paths != expected_paths:
        raise CatalogValidationError(
            'catalog directory must contain five service files'
        )
    catalogs = tuple(
        _parse_catalog(
            directory / f'{service.value}.xml',
            service,
            forbidden_values,
        )
        for service in SERVICE_ORDER
    )
    pairs = tuple(pair for catalog in catalogs for pair in catalog.pairs)
    if len({pair.task_id for pair in pairs}) != len(pairs):
        raise CatalogValidationError('catalog task ids must be unique')
    if len({pair.question for pair in pairs}) != len(pairs):
        raise CatalogValidationError('catalog questions must be unique')
    refs = {logical_ref for pair in pairs for logical_ref in pair.fixture_refs}
    if refs != EXPECTED_LOGICAL_REFS:
        raise CatalogValidationError('catalog fixture coverage is incomplete')
    for catalog in catalogs:
        tools = {tool for pair in catalog.pairs for tool in pair.allowed_tools}
        if tools != READONLY_TOOLS[catalog.service]:
            raise CatalogValidationError(
                f'{catalog.service.value} tool coverage is incomplete'
            )
    return catalogs
