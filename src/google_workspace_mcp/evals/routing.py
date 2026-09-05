"""Enforce evaluation tool routes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .catalog import (
    CALENDAR_TIMED_END_UTC,
    CALENDAR_TIMED_START_UTC,
    OBJECTS_BY_REF,
)
from .models import FixtureBindings
from .validation import EvaluationPair

# === Constants ===

CALENDAR_PRIMARY_ALIAS = 'calendar_primary'
_IDENTIFIER_FIELDS = frozenset(
    {
        'calendar_id',
        'calendar_ids',
        'document_id',
        'draft_id',
        'event_id',
        'file_id',
        'folder_id',
        'message_id',
        'parent_id',
        'recurring_event_id',
        'spreadsheet_id',
        'tab_id',
        'thread_id',
    }
)
_BINDING_FIELDS = {
    'draft_id': 'draft_id',
    'document_id': 'document_id',
    'event_id': 'event_id',
    'file_id': 'file_id',
    'folder_id': 'file_id',
    'message_id': 'message_id',
    'parent_id': 'file_id',
    'recurring_event_id': 'event_id',
    'spreadsheet_id': 'spreadsheet_id',
    'tab_id': 'tab_id',
    'thread_id': 'thread_id',
}
_MARKER_SEARCH_TOOLS = frozenset(
    {
        'gmail_search_messages',
        'gmail_search_threads',
        'calendar_search_events',
    }
)
_SAFE_FIELDS = frozenset(
    {
        'access_role',
        'blocks',
        'body_text',
        'bullet',
        'busy',
        'calendar_id',
        'calendars',
        'cell_count',
        'cells',
        'child_count',
        'children',
        'column_count',
        'content',
        'date',
        'date_time',
        'document_id',
        'draft_id',
        'end',
        'end_index',
        'elements',
        'errors',
        'event_id',
        'file_id',
        'files',
        'groups',
        'items',
        'index',
        'kind',
        'label_id',
        'label_ids',
        'label_type',
        'list_id',
        'major_dimension',
        'message',
        'message_count',
        'message_id',
        'messages',
        'mime_type',
        'name',
        'named_style',
        'nesting_level',
        'next_page_token',
        'next_start_index',
        'original_start',
        'parent_tab_id',
        'parents',
        'primary',
        'ranges',
        'recurrence',
        'recurring_event_id',
        'requested_range',
        'resolved_range',
        'row_count',
        'rows',
        'sheet_id',
        'sheet_type',
        'sheets',
        'snippet',
        'spreadsheet_id',
        'start',
        'start_index',
        'status',
        'subject',
        'summary',
        'tab_id',
        'tabs',
        'text_characters',
        'thread_id',
        'time_max',
        'time_min',
        'time_zone',
        'title',
        'truncated',
        'unsupported_kind',
        'unsupported_kinds',
        'values',
    }
)
_EMAIL_PATTERN = re.compile(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b')
_OUTPUT_IDENTIFIER_FIELDS = frozenset(
    {
        'calendar_id',
        'document_id',
        'draft_id',
        'event_id',
        'file_id',
        'message_id',
        'parent_tab_id',
        'parents',
        'recurring_event_id',
        'sheet_id',
        'spreadsheet_id',
        'tab_id',
        'thread_id',
    }
)


class RouteValidationError(ValueError):
    """Report an invalid tool route."""


def _binding_value(
    pair: EvaluationPair,
    logical_ref: str,
    field_name: str,
    bindings: FixtureBindings,
) -> str | int:
    """Resolve one pair local identifier."""
    if logical_ref not in pair.fixture_refs:
        raise RouteValidationError('logical ref is outside the pair')
    binding = bindings.objects.get(logical_ref)
    if binding is None:
        raise RouteValidationError('logical ref is not bound')
    identifier_field = _BINDING_FIELDS[field_name]
    value = getattr(binding.identifiers, identifier_field)
    if not isinstance(value, str | int) or value == '':
        raise RouteValidationError('logical ref lacks the required identifier')
    return value


def _calendar_primary(bindings: FixtureBindings) -> str:
    """Resolve the primary calendar alias."""
    primary = bindings.calendar_primary_id
    if primary is None or not primary.get_secret_value():
        raise RouteValidationError('primary calendar is not bound')
    return primary.get_secret_value()


def _resolve_identifier(
    pair: EvaluationPair,
    field_name: str,
    value: Any,
    bindings: FixtureBindings,
) -> Any:
    """Resolve one identifier input."""
    if field_name == 'calendar_id':
        if value != CALENDAR_PRIMARY_ALIAS:
            raise RouteValidationError('identifier must be a logical ref')
        return _calendar_primary(bindings)
    if field_name == 'calendar_ids':
        if not isinstance(value, list) or value != [CALENDAR_PRIMARY_ALIAS]:
            raise RouteValidationError('identifier must be a logical ref')
        return [_calendar_primary(bindings)]
    if not isinstance(value, str) or value not in OBJECTS_BY_REF:
        raise RouteValidationError('identifier must be a logical ref')
    return _binding_value(pair, value, field_name, bindings)


def _pair_markers(pair: EvaluationPair) -> tuple[str, ...]:
    """Return public pair markers."""
    return tuple(
        marker
        for logical_ref in pair.fixture_refs
        if (marker := OBJECTS_BY_REF[logical_ref].marker) is not None
    )


def _validate_search_scope(
    pair: EvaluationPair,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    """Require a public marker on broad searches."""
    if tool_name == 'drive_search_files':
        markers = _pair_markers(pair)
        search_values = (
            arguments.get('exact_name'),
            arguments.get('text'),
        )
        if any(
            isinstance(value, str)
            and any(marker in value for marker in markers)
            for value in search_values
        ):
            return
        parent_id = arguments.get('parent_id')
        if isinstance(parent_id, str) and parent_id in pair.fixture_refs:
            return
        raise RouteValidationError('Drive search must use a fixture boundary')
    if tool_name not in _MARKER_SEARCH_TOOLS:
        return
    query = arguments.get('query')
    markers = _pair_markers(pair)
    if not isinstance(query, str) or not any(
        marker in query for marker in markers
    ):
        raise RouteValidationError('search must use a fixture marker')


def _validate_freebusy_scope(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    """Require the exact synthetic busy window."""
    if tool_name != 'calendar_get_freebusy':
        return
    if (
        arguments.get('calendar_ids') != [CALENDAR_PRIMARY_ALIAS]
        or arguments.get('time_min') != CALENDAR_TIMED_START_UTC
        or arguments.get('time_max') != CALENDAR_TIMED_END_UTC
        or arguments.get('time_zone', 'UTC') != 'UTC'
    ):
        raise RouteValidationError('free busy scope is outside the fixture')


def resolve_tool_arguments(
    pair: EvaluationPair,
    tool_name: str,
    arguments: Mapping[str, Any],
    bindings: FixtureBindings,
    *,
    allowed_page_tokens: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve one allowed tool request."""
    if tool_name not in pair.allowed_tools:
        raise RouteValidationError('tool is not allowed for the pair')
    if not isinstance(arguments, Mapping):
        raise RouteValidationError('tool arguments must be an object')
    drive_id = arguments.get('drive_id')
    if drive_id is not None and drive_id != '':
        raise RouteValidationError('shared drive IDs are forbidden')
    page_tokens = allowed_page_tokens or {}
    page_token = arguments.get('page_token')
    if (
        page_token is not None
        and page_token != ''
        and (
            tool_name != 'gmail_list_drafts'
            or not isinstance(page_token, str)
            or page_token not in page_tokens
        )
    ):
        raise RouteValidationError('page token was not issued in this pair')
    _validate_search_scope(pair, tool_name, arguments)
    _validate_freebusy_scope(tool_name, arguments)
    resolved: dict[str, Any] = {}
    for name, value in arguments.items():
        if name == 'page_token' and value:
            resolved[name] = page_tokens[value]
        elif name in _IDENTIFIER_FIELDS:
            resolved[name] = _resolve_identifier(
                pair,
                name,
                value,
                bindings,
            )
        else:
            resolved[name] = value
    return resolved


def _reverse_identifiers(
    pair: EvaluationPair,
    bindings: FixtureBindings,
) -> dict[str | int, str]:
    """Build a pair preferred reverse map."""
    ordered_refs = (*pair.fixture_refs, *bindings.objects)
    reverse: dict[str | int, str] = {}
    for logical_ref in reversed(ordered_refs):
        binding = bindings.objects.get(logical_ref)
        if binding is None:
            continue
        for value in binding.identifiers.model_dump().values():
            if isinstance(value, str | int) and value != '':
                reverse[value] = logical_ref
    primary = bindings.calendar_primary_id
    if primary is not None and primary.get_secret_value():
        reverse[primary.get_secret_value()] = CALENDAR_PRIMARY_ALIAS
    return reverse


def _sanitize_value(
    value: Any,
    reverse: Mapping[str | int, str],
    blocked_fields: frozenset[str],
    page_token_aliases: Mapping[str, str],
) -> Any:
    """Build one minimal structured projection."""
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if key not in _SAFE_FIELDS or key in blocked_fields:
                continue
            if key in _OUTPUT_IDENTIFIER_FIELDS:
                if isinstance(item, list):
                    mapped_items = [
                        reverse[value] for value in item if value in reverse
                    ]
                    if mapped_items:
                        projected[str(key)] = mapped_items
                    continue
                if item not in reverse:
                    continue
            if key == 'next_page_token':
                if not isinstance(item, str) or item not in page_token_aliases:
                    continue
                projected[str(key)] = page_token_aliases[item]
                continue
            projected[str(key)] = _sanitize_value(
                item,
                reverse,
                blocked_fields,
                page_token_aliases,
            )
        return projected
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [
            _sanitize_value(
                item,
                reverse,
                blocked_fields,
                page_token_aliases,
            )
            for item in value
        ]
    if isinstance(value, str | int) and value in reverse:
        return reverse[value]
    return value


def _contains_pair_value(value: Any, pair: EvaluationPair) -> bool:
    """Detect one pair local result."""
    if isinstance(value, Mapping):
        return any(_contains_pair_value(item, pair) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_contains_pair_value(item, pair) for item in value)
    if isinstance(value, str):
        return value in pair.fixture_refs or any(
            marker in value for marker in _pair_markers(pair)
        )
    return False


def _filter_search_results(
    tool_name: str,
    projected: dict[str, Any],
    pair: EvaluationPair,
) -> dict[str, Any]:
    """Remove results outside the pair."""
    collection_name = 'files' if tool_name.startswith('drive_') else 'items'
    collection = projected.get(collection_name)
    if isinstance(collection, list):
        projected[collection_name] = [
            item for item in collection if _contains_pair_value(item, pair)
        ]
    return projected


def _project_labels(structured: Mapping[str, Any]) -> dict[str, Any]:
    """Project sorted system labels."""
    items = structured.get('items')
    if not isinstance(items, list):
        return {'items': []}
    projected = [
        {
            'label_id': item.get('label_id'),
            'name': item.get('name'),
            'label_type': item.get('label_type'),
        }
        for item in items
        if isinstance(item, Mapping) and item.get('label_type') == 'system'
    ]
    projected.sort(key=lambda item: str(item['label_id']))
    return {'items': projected}


def _project_calendars(
    structured: Mapping[str, Any],
    reverse: Mapping[str | int, str],
) -> dict[str, Any]:
    """Project only primary calendar metadata."""
    items = structured.get('items')
    if not isinstance(items, list):
        return {'items': []}
    projected = []
    for item in items:
        if not isinstance(item, Mapping) or item.get('primary') is not True:
            continue
        calendar_id = item.get('calendar_id')
        if (
            not isinstance(calendar_id, str | int)
            or calendar_id not in reverse
        ):
            continue
        projected.append(
            {
                'calendar_id': reverse[calendar_id],
                'primary': True,
                'time_zone': item.get('time_zone'),
                'access_role': item.get('access_role'),
            }
        )
    return {'items': projected}


def _project_freebusy(structured: Mapping[str, Any]) -> dict[str, Any]:
    """Project only fixture window occupancy."""
    calendars = structured.get('calendars')
    if not isinstance(calendars, list):
        return {'busy': False}
    window_start = _parse_rfc3339(CALENDAR_TIMED_START_UTC)
    window_end = _parse_rfc3339(CALENDAR_TIMED_END_UTC)
    if window_start is None or window_end is None:
        raise RouteValidationError('fixture busy window is invalid')
    whole_window_busy = any(
        _period_covers_window(period, window_start, window_end)
        for calendar in calendars
        if isinstance(calendar, Mapping)
        for period in calendar.get('busy', [])
        if isinstance(calendar.get('busy'), list)
    )
    return {'busy': whole_window_busy}


def _period_covers_window(
    period: object,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Check one busy period against the window."""
    if not isinstance(period, Mapping):
        return False
    period_start = _parse_rfc3339(period.get('start'))
    period_end = _parse_rfc3339(period.get('end'))
    return bool(
        period_start is not None
        and period_end is not None
        and period_start <= window_start
        and period_end >= window_end
    )


def _parse_rfc3339(value: object) -> datetime | None:
    """Parse one aware RFC3339 instant."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _assert_projection_safe(
    projected: dict[str, Any],
    bindings: FixtureBindings,
) -> None:
    """Reject private values after projection."""
    serialized = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    private_values = {
        value
        for binding in bindings.objects.values()
        for value in binding.identifiers.model_dump().values()
        if isinstance(value, str) and value
    }
    if bindings.owner_email is not None:
        private_values.add(bindings.owner_email.get_secret_value())
    if bindings.calendar_primary_id is not None:
        private_values.add(bindings.calendar_primary_id.get_secret_value())
    if any(value and value in serialized for value in private_values):
        raise RouteValidationError('projection contains a private value')
    if (
        _EMAIL_PATTERN.search(serialized)
        or 'http://' in serialized
        or 'https://' in serialized
    ):
        raise RouteValidationError('projection contains private content')


def project_tool_result(
    pair: EvaluationPair,
    tool_name: str,
    structured: object,
    bindings: FixtureBindings,
    *,
    page_token_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project one structured tool result."""
    if not isinstance(structured, Mapping):
        raise RouteValidationError('structured content is required')
    reverse = _reverse_identifiers(pair, bindings)
    if tool_name == 'gmail_list_labels':
        projected = _project_labels(structured)
    elif tool_name == 'calendar_list_calendars':
        projected = _project_calendars(structured, reverse)
    elif tool_name == 'calendar_get_freebusy':
        projected = _project_freebusy(structured)
    else:
        projected_value = _sanitize_value(
            structured,
            reverse,
            frozenset({'date'})
            if tool_name.startswith('gmail_')
            else frozenset(),
            page_token_aliases or {},
        )
        if not isinstance(projected_value, dict):
            raise RouteValidationError('structured content is required')
        projected = projected_value
    if tool_name in {
        'gmail_search_messages',
        'gmail_search_threads',
        'gmail_list_drafts',
        'calendar_search_events',
        'calendar_list_event_instances',
        'drive_search_files',
        'drive_list_folder',
    }:
        projected = _filter_search_results(tool_name, projected, pair)
    _assert_projection_safe(projected, bindings)
    return projected
