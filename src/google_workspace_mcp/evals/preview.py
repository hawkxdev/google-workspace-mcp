"""Create immutable write previews."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .catalog import EXPECTED_LOGICAL_REFS, OBJECTS_BY_REF
from .models import (
    FIXTURE_VERSION,
    ApplicationConfirmation,
    BindingState,
    FixtureBindings,
    FixturePreview,
    PreviewOperation,
)
from .requests import (
    PreparedOperation,
    ServiceFactory,
    build_preview_services,
    build_write_operations,
)

# === Errors ===


class PartialFixtureStateError(ValueError):
    """Reject unsafe partial application."""


class ApplicationConfirmationError(ValueError):
    """Reject invalid application confirmation."""


# === Prepared preview ===


@dataclass(frozen=True, slots=True)
class PreparedPreview:
    """Hold preview and requests."""

    document: FixturePreview
    operations: tuple[PreparedOperation, ...]


def _digest(operations: tuple[PreviewOperation, ...]) -> str:
    """Hash canonical preview operations."""
    payload = [operation.model_dump(mode='json') for operation in operations]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def build_preview(
    bindings: FixtureBindings | None = None,
    *,
    service_factory: ServiceFactory = build_preview_services,
) -> PreparedPreview:
    """Build complete remaining preview."""
    state = bindings.state if bindings is not None else BindingState.PLANNED
    known_refs = (
        frozenset(bindings.objects) if bindings is not None else frozenset()
    )
    applied_operations = (
        bindings.applied_operations if bindings is not None else frozenset()
    )
    unknown_refs = known_refs - EXPECTED_LOGICAL_REFS
    if unknown_refs:
        names = ', '.join(sorted(unknown_refs))
        raise ValueError(f'bindings contain unknown logical refs: {names}')
    if bindings is not None:
        for logical_ref, binding in bindings.objects.items():
            fixture_object = OBJECTS_BY_REF[logical_ref]
            if (
                binding.service is not fixture_object.service
                or binding.resource_kind is not fixture_object.resource_kind
            ):
                raise ValueError('binding does not match fixture catalog')

    all_operations = build_write_operations(service_factory())
    operation_ids = frozenset(
        operation.preview.operation_id for operation in all_operations
    )
    unknown_operations = applied_operations - operation_ids
    if unknown_operations:
        names = ', '.join(sorted(unknown_operations))
        raise ValueError(f'bindings contain unknown operations: {names}')
    remaining: list[PreparedOperation] = []
    partial_output = False
    for prepared in all_operations:
        outputs = frozenset(prepared.preview.logical_outputs)
        operation_id = prepared.preview.operation_id
        if operation_id in applied_operations:
            if outputs and not outputs.issubset(known_refs):
                partial_output = True
            continue
        status = prepared.preview.status
        if outputs & known_refs:
            status = 'blocked_partial_output'
            partial_output = True
        remaining.append(
            PreparedOperation(
                request=prepared.request,
                preview=prepared.preview.model_copy(update={'status': status}),
            )
        )

    missing_refs = tuple(sorted(EXPECTED_LOGICAL_REFS - known_refs))
    missing_operations = operation_ids - applied_operations
    started = bool(known_refs or applied_operations)
    partial_state = started and bool(
        missing_refs or missing_operations or partial_output
    )
    previews = tuple(item.preview for item in remaining)
    digest = _digest(previews)
    confirmation = f'apply {FIXTURE_VERSION} {digest}'
    document = FixturePreview(
        fixture_version=FIXTURE_VERSION,
        binding_state=state,
        application_allowed=not partial_state and not started,
        blocked_reason='partial_state' if partial_state else None,
        operation_count=len(previews),
        missing_logical_refs=missing_refs,
        preview_digest=digest,
        confirmation=confirmation,
        operations=previews,
    )
    return PreparedPreview(document=document, operations=tuple(remaining))


def confirm_application(
    preview: PreparedPreview,
    confirmation: ApplicationConfirmation,
) -> tuple[PreparedOperation, ...]:
    """Validate explicit apply confirmation."""
    document = preview.document
    if document.blocked_reason == 'partial_state':
        raise PartialFixtureStateError(
            'partial fixture state requires review of the remaining preview'
        )
    if not document.application_allowed:
        raise ApplicationConfirmationError('preview has nothing safe to apply')
    if confirmation.fixture_version != document.fixture_version:
        raise ApplicationConfirmationError('fixture version does not match')
    if confirmation.preview_digest != document.preview_digest:
        raise ApplicationConfirmationError('preview digest does not match')
    return preview.operations
