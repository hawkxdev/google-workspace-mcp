"""Test complete mutation preview."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from googleapiclient.discovery import build

from google_workspace_mcp.evals.models import (
    ApplicationConfirmation,
    BindingState,
)
from google_workspace_mcp.evals.preview import (
    ApplicationConfirmationError,
    PartialFixtureStateError,
    build_preview,
    confirm_application,
)
from google_workspace_mcp.evals.requests import GoogleServiceSet

from .conftest import EXPECTED_OPERATION_IDS, make_bindings


class ExecutionTrapHttp:
    """Fail every provider execution."""

    def __init__(self) -> None:
        """Initialize execution counter."""
        self.calls = 0

    def request(self, *_args: Any, **_kwargs: Any) -> Any:
        """Reject every network request."""
        self.calls += 1
        raise AssertionError('Google request execution is forbidden')


def test_preview_contains_every_planned_write() -> None:
    preview = build_preview().document

    operation_ids = tuple(
        operation.operation_id for operation in preview.operations
    )
    service_counts = Counter(
        operation.service.value for operation in preview.operations
    )

    assert operation_ids == EXPECTED_OPERATION_IDS
    assert service_counts == {
        'gmail': 4,
        'calendar': 3,
        'drive': 3,
        'sheets': 2,
        'docs': 2,
    }
    assert preview.operation_count == 14
    assert all(operation.method == 'POST' for operation in preview.operations)


def test_preview_never_executes_google_request() -> None:
    transport = ExecutionTrapHttp()

    def service_factory() -> GoogleServiceSet:
        """Build execution trap clients."""
        common = {
            'http': transport,
            'cache_discovery': False,
            'static_discovery': True,
        }
        return GoogleServiceSet(
            gmail=build('gmail', 'v1', **common),
            calendar=build('calendar', 'v3', **common),
            drive=build('drive', 'v3', **common),
            sheets=build('sheets', 'v4', **common),
            docs=build('docs', 'v1', **common),
        )

    preview = build_preview(service_factory=service_factory)

    assert preview.document.operation_count == 14
    assert transport.calls == 0


def test_preview_redacts_private_owner_address() -> None:
    bindings = make_bindings(
        state=BindingState.PLANNED,
        logical_refs=frozenset(),
        applied_operations=frozenset(),
        owner_email='owner@confidential.invalid',
    )

    output = build_preview(bindings).document.model_dump_json()

    assert 'owner@confidential.invalid' not in output
    assert 'confidential.invalid' not in output
    assert 'bindings.owner_email' in output


def test_partial_state_refuses_application() -> None:
    bindings = make_bindings(
        logical_refs=frozenset({'drive_fixture_folder'}),
        applied_operations=frozenset({'drive.create.folder'}),
    )
    preview = build_preview(bindings)
    confirmation = ApplicationConfirmation(
        fixture_version='stage12-v1',
        preview_digest=preview.document.preview_digest,
        acknowledge_writes=True,
    )

    assert preview.document.blocked_reason == 'partial_state'
    assert preview.document.application_allowed is False
    assert preview.document.operation_count == 13
    assert 'drive_fixture_folder' not in (
        preview.document.missing_logical_refs
    )
    with pytest.raises(
        PartialFixtureStateError,
        match='partial fixture state',
    ):
        confirm_application(preview, confirmation)


def test_partial_operation_output_is_blocked() -> None:
    bindings = make_bindings(
        logical_refs=frozenset({'gmail_draft_cobalt'}),
        applied_operations=frozenset(),
    )

    preview = build_preview(bindings).document

    draft = preview.operations[0]
    assert draft.operation_id == 'gmail.create_draft.cobalt'
    assert draft.status == 'blocked_partial_output'
    assert preview.blocked_reason == 'partial_state'


def test_complete_applied_registry_has_no_remaining_writes() -> None:
    preview = build_preview(make_bindings()).document

    assert preview.operation_count == 0
    assert preview.operations == ()
    assert preview.missing_logical_refs == ()
    assert preview.application_allowed is False


def test_confirmation_requires_exact_preview_digest() -> None:
    preview = build_preview()
    confirmation = ApplicationConfirmation(
        fixture_version='stage12-v1',
        preview_digest='0' * 64,
        acknowledge_writes=True,
    )

    with pytest.raises(
        ApplicationConfirmationError,
        match='digest does not match',
    ):
        confirm_application(preview, confirmation)


def test_confirmation_returns_requests_without_execution() -> None:
    preview = build_preview()
    confirmation = ApplicationConfirmation(
        fixture_version='stage12-v1',
        preview_digest=preview.document.preview_digest,
        acknowledge_writes=True,
    )

    operations = confirm_application(preview, confirmation)

    assert len(operations) == 14
    assert all(operation.request.uri for operation in operations)
