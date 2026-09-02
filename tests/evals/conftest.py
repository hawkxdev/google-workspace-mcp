"""Provide evaluation fixture data."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from google_workspace_mcp.evals.catalog import FIXTURE_OBJECTS
from google_workspace_mcp.evals.models import (
    FIXTURE_VERSION,
    BindingState,
    CredentialReference,
    FixtureBindings,
    ObjectBinding,
    ProviderIdentifiers,
    ResourceKind,
    ServiceName,
)

EXPECTED_OPERATION_IDS = (
    'gmail.create_draft.cobalt',
    'gmail.send_message.alpha_root',
    'gmail.send_message.alpha_reply',
    'gmail.send_message.beta_root',
    'calendar.create_event.timed',
    'calendar.create_event.all_day',
    'calendar.create_event.recurring',
    'drive.create.folder',
    'drive.create.note',
    'drive.create.ledger',
    'sheets.create.primary',
    'sheets.write.primary_values',
    'docs.create.primary',
    'docs.write.primary_text',
)


def _credentials() -> dict[ServiceName, CredentialReference]:
    """Build private credential references."""
    return {
        service: CredentialReference(
            service=service,
            reference=f'oauth/{service.value}.json',
        )
        for service in ServiceName
    }


def _identifiers(
    logical_ref: str,
    resource_kind: ResourceKind,
) -> ProviderIdentifiers:
    """Build kind-specific provider identifiers."""
    token = logical_ref.replace('_', '-')
    values: dict[str, Any]
    match resource_kind:
        case ResourceKind.GMAIL_DRAFT:
            values = {'draft_id': f'draft-{token}'}
        case ResourceKind.GMAIL_MESSAGE:
            values = {
                'message_id': f'message-{token}',
                'message_header_id': f'header-{token}',
            }
        case ResourceKind.GMAIL_THREAD:
            values = {'thread_id': f'thread-{token}'}
        case ResourceKind.GMAIL_DELIVERY:
            values = {
                'message_id': f'delivery-message-{token}',
                'thread_id': f'delivery-thread-{token}',
            }
        case ResourceKind.CALENDAR_EVENT:
            values = {'event_id': f'event-{token}'}
        case ResourceKind.DRIVE_FOLDER | ResourceKind.DRIVE_FILE:
            values = {'file_id': f'file-{token}'}
        case ResourceKind.SHEETS_SPREADSHEET:
            values = {'spreadsheet_id': f'spreadsheet-{token}'}
        case ResourceKind.SHEETS_TAB:
            values = {
                'spreadsheet_id': 'spreadsheet-sheets-primary',
                'sheet_id': 41001
                if logical_ref.endswith('inputs_tab')
                else 41002,
            }
        case ResourceKind.DOCS_DOCUMENT:
            values = {'document_id': f'document-{token}'}
        case ResourceKind.DOCS_TAB:
            values = {
                'document_id': 'document-docs-primary',
                'tab_id': f'tab-{token}',
            }
    return ProviderIdentifiers.model_validate(values)


def make_bindings(
    *,
    state: BindingState = BindingState.APPLIED,
    logical_refs: frozenset[str] | None = None,
    applied_operations: frozenset[str] | None = None,
    owner_email: str | None = None,
) -> FixtureBindings:
    """Build private binding registry."""
    selected_refs = logical_refs
    if selected_refs is None:
        selected_refs = frozenset(
            fixture_object.logical_ref for fixture_object in FIXTURE_OBJECTS
        )
    objects = {
        fixture_object.logical_ref: ObjectBinding(
            logical_ref=fixture_object.logical_ref,
            service=fixture_object.service,
            resource_kind=fixture_object.resource_kind,
            identifiers=_identifiers(
                fixture_object.logical_ref,
                fixture_object.resource_kind,
            ),
        )
        for fixture_object in FIXTURE_OBJECTS
        if fixture_object.logical_ref in selected_refs
    }
    completed = applied_operations
    if completed is None:
        completed = frozenset(EXPECTED_OPERATION_IDS)
    return FixtureBindings(
        fixture_version=FIXTURE_VERSION,
        state=state,
        owner_email=owner_email,
        calendar_primary_id='primary-calendar-private-value',
        credentials=_credentials(),
        objects=objects,
        applied_operations=completed,
    )


@pytest.fixture
def applied_bindings() -> FixtureBindings:
    """Provide fully applied bindings."""
    return make_bindings()


@pytest.fixture
def protected_json_file(tmp_path: Path) -> Iterator[Path]:
    """Create protected JSON file."""
    target = tmp_path / 'bindings.json'
    target.touch(mode=0o600)
    yield target
