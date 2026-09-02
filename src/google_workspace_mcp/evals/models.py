"""Define evaluation fixture models."""

from __future__ import annotations

import json
import os
import stat
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

# === Constants ===

FIXTURE_VERSION: Literal['stage12-v1'] = 'stage12-v1'


class FixtureModel(BaseModel):
    """Configure immutable fixture models."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class ServiceName(StrEnum):
    """Name one Google service."""

    GMAIL = 'gmail'
    CALENDAR = 'calendar'
    DRIVE = 'drive'
    SHEETS = 'sheets'
    DOCS = 'docs'


class BindingState(StrEnum):
    """Track fixture lifecycle state."""

    PLANNED = 'planned'
    APPLIED = 'applied'
    READY = 'ready'


class ResourceKind(StrEnum):
    """Classify bound Google resources."""

    GMAIL_DRAFT = 'gmail_draft'
    GMAIL_MESSAGE = 'gmail_message'
    GMAIL_THREAD = 'gmail_thread'
    GMAIL_DELIVERY = 'gmail_delivery'
    CALENDAR_EVENT = 'calendar_event'
    DRIVE_FOLDER = 'drive_folder'
    DRIVE_FILE = 'drive_file'
    SHEETS_SPREADSHEET = 'sheets_spreadsheet'
    SHEETS_TAB = 'sheets_tab'
    DOCS_DOCUMENT = 'docs_document'
    DOCS_TAB = 'docs_tab'


class ProviderIdentifiers(FixtureModel):
    """Store derived provider identifiers."""

    draft_id: str | None = None
    message_id: str | None = None
    message_header_id: str | None = None
    thread_id: str | None = None
    event_id: str | None = None
    file_id: str | None = None
    spreadsheet_id: str | None = None
    sheet_id: int | None = None
    document_id: str | None = None
    tab_id: str | None = None

    @field_validator(
        'draft_id',
        'message_id',
        'message_header_id',
        'thread_id',
        'event_id',
        'file_id',
        'spreadsheet_id',
        'document_id',
        'tab_id',
    )
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        """Reject empty provider identifiers."""
        if value is not None and (not value.strip() or len(value) > 1024):
            raise ValueError('provider identifiers must be non-empty')
        return value

    def populated_fields(self) -> frozenset[str]:
        """Return populated identifier fields."""
        return frozenset(
            name
            for name, value in self.model_dump().items()
            if value is not None
        )


_REQUIRED_IDENTIFIER_FIELDS: dict[ResourceKind, frozenset[str]] = {
    ResourceKind.GMAIL_DRAFT: frozenset({'draft_id'}),
    ResourceKind.GMAIL_MESSAGE: frozenset({'message_id'}),
    ResourceKind.GMAIL_THREAD: frozenset({'thread_id'}),
    ResourceKind.GMAIL_DELIVERY: frozenset({'message_id', 'thread_id'}),
    ResourceKind.CALENDAR_EVENT: frozenset({'event_id'}),
    ResourceKind.DRIVE_FOLDER: frozenset({'file_id'}),
    ResourceKind.DRIVE_FILE: frozenset({'file_id'}),
    ResourceKind.SHEETS_SPREADSHEET: frozenset({'spreadsheet_id'}),
    ResourceKind.SHEETS_TAB: frozenset({'spreadsheet_id', 'sheet_id'}),
    ResourceKind.DOCS_DOCUMENT: frozenset({'document_id'}),
    ResourceKind.DOCS_TAB: frozenset({'document_id', 'tab_id'}),
}


class ObjectBinding(FixtureModel):
    """Bind one logical resource."""

    logical_ref: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]+$')]
    service: ServiceName
    resource_kind: ResourceKind
    identifiers: ProviderIdentifiers

    @model_validator(mode='after')
    def validate_identifiers(self) -> ObjectBinding:
        """Require kind-specific provider identifiers."""
        expected_service = self.resource_kind.value.split('_', maxsplit=1)[0]
        if self.service.value != expected_service:
            raise ValueError('resource kind does not match service')
        required = _REQUIRED_IDENTIFIER_FIELDS[self.resource_kind]
        if not required.issubset(self.identifiers.populated_fields()):
            names = ', '.join(sorted(required))
            raise ValueError(
                f'{self.resource_kind.value} requires identifiers: {names}'
            )
        return self


class CredentialReference(FixtureModel):
    """Reference one private credential file."""

    service: ServiceName
    kind: Literal['oauth_user'] = 'oauth_user'
    reference: Annotated[
        str,
        Field(pattern=r'^oauth/(gmail|calendar|drive|sheets|docs)\.json$'),
    ]

    @model_validator(mode='after')
    def validate_service_path(self) -> CredentialReference:
        """Match service and credential path."""
        expected = f'oauth/{self.service.value}.json'
        if self.reference != expected:
            raise ValueError('credential reference does not match service')
        return self


class FixtureBindings(FixtureModel):
    """Describe private fixture bindings."""

    fixture_version: Literal['stage12-v1']
    state: BindingState
    owner_email: SecretStr | None = None
    calendar_primary_id: SecretStr | None = None
    credentials: dict[ServiceName, CredentialReference]
    objects: dict[str, ObjectBinding] = Field(default_factory=dict)
    applied_operations: frozenset[str] = Field(default_factory=frozenset)

    @model_validator(mode='after')
    def validate_registry(self) -> FixtureBindings:
        """Validate private registry consistency."""
        expected_services = set(ServiceName)
        if set(self.credentials) != expected_services:
            raise ValueError('credentials must reference all five services')
        for service, reference in self.credentials.items():
            if service is not reference.service:
                raise ValueError('credential key does not match service')
        for logical_ref, binding in self.objects.items():
            if logical_ref != binding.logical_ref:
                raise ValueError('object key does not match logical_ref')
        if self.state is BindingState.READY and not self.objects:
            raise ValueError('ready bindings cannot have an empty registry')
        return self


class PreviewOperation(FixtureModel):
    """Describe one future mutation."""

    operation_id: str
    service: ServiceName
    action: str
    status: Literal['planned', 'blocked_partial_output']
    logical_outputs: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    private_references: tuple[str, ...] = ()
    method: str
    uri: str
    body: dict[str, Any]


class FixturePreview(FixtureModel):
    """Describe complete write preview."""

    fixture_version: Literal['stage12-v1']
    binding_state: BindingState
    application_allowed: bool
    blocked_reason: Literal['partial_state'] | None
    operation_count: int
    missing_logical_refs: tuple[str, ...]
    preview_digest: str
    confirmation: str
    operations: tuple[PreviewOperation, ...]


class ApplicationConfirmation(FixtureModel):
    """Confirm one immutable preview."""

    fixture_version: Literal['stage12-v1']
    preview_digest: str
    acknowledge_writes: Literal[True]


class ReadinessItem(FixtureModel):
    """Report one readiness observation."""

    logical_ref: str
    status: Literal['ready', 'not_ready']
    probe: str


class ReadinessReport(FixtureModel):
    """Report fixture readiness pass."""

    fixture_version: Literal['stage12-v1']
    status: Literal['ready', 'not_ready']
    probe_count: int
    items: tuple[ReadinessItem, ...]


# === Loading ===


def load_bindings(path: Path) -> FixtureBindings:
    """Load protected private bindings."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError('bindings file is unavailable') from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError('bindings path must be a regular file')
        if file_stat.st_uid != os.getuid():
            raise ValueError('bindings file must belong to the current user')
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise ValueError('bindings file mode must be 0600')
        with os.fdopen(descriptor, encoding='utf-8') as bindings_file:
            descriptor = -1
            payload = json.load(bindings_file)
    except json.JSONDecodeError as error:
        raise ValueError('bindings file must contain valid JSON') from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return FixtureBindings.model_validate(payload)
