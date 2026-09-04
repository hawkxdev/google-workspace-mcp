"""Verify synthetic fixture readiness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from googleapiclient.errors import HttpError

from .catalog import EXPECTED_LOGICAL_REFS, OBJECTS_BY_REF
from .models import (
    FIXTURE_VERSION,
    BindingState,
    FixtureBindings,
    ObjectBinding,
    ReadinessItem,
    ReadinessReport,
    ResourceKind,
)
from .requests import WRITE_OPERATION_IDS, GoogleServiceSet

# === Probe contract ===


class FixtureReadinessError(RuntimeError):
    """Report failed readiness checks."""


class ReadinessProbe(Protocol):
    """Read fixture objects once."""

    def find_gmail_delivery(
        self,
        *,
        exact_marker: str,
        max_results: int,
    ) -> bool:
        """Find one delivered Gmail message."""
        ...

    def object_exists(self, binding: ObjectBinding) -> bool:
        """Read one bound Google object."""
        ...


class GoogleReadinessProbe:
    """Perform bounded provider reads."""

    def __init__(
        self,
        services: GoogleServiceSet,
        *,
        calendar_primary_id: str,
    ) -> None:
        """Initialize provider readiness probe."""
        if not calendar_primary_id:
            raise ValueError('calendar primary ID is required')
        self._services = services
        self._calendar_primary_id = calendar_primary_id

    @staticmethod
    def _execute(request: Any) -> Mapping[str, Any] | None:
        """Execute one readonly request."""
        try:
            value = request.execute(num_retries=0)
        except HttpError as error:
            if int(getattr(error.resp, 'status', 0)) == 404:
                return None
            raise
        if not isinstance(value, Mapping):
            raise ValueError('Google readiness response must be an object')
        return value

    def find_gmail_delivery(
        self,
        *,
        exact_marker: str,
        max_results: int,
    ) -> bool:
        """Search one exact Gmail marker."""
        if max_results != 1:
            raise ValueError('Gmail readiness search must request one result')
        request = (
            self._services.gmail.users()
            .messages()
            .list(
                userId='me',
                q=f'"{exact_marker}"',
                maxResults=1,
                fields='messages(id,threadId)',
            )
        )
        result = self._execute(request)
        if result is None:
            return False
        messages = result.get('messages') or ()
        if not isinstance(messages, Sequence) or isinstance(
            messages, str | bytes
        ):
            raise ValueError('Gmail readiness response is invalid')
        return len(messages) == 1

    def object_exists(self, binding: ObjectBinding) -> bool:
        """Read one bound Google object."""
        identifiers = binding.identifiers
        match binding.resource_kind:
            case ResourceKind.GMAIL_DRAFT:
                request = (
                    self._services.gmail.users()
                    .drafts()
                    .get(
                        userId='me',
                        id=identifiers.draft_id,
                        format='minimal',
                        fields='id,message(id,threadId)',
                    )
                )
            case ResourceKind.GMAIL_MESSAGE:
                request = (
                    self._services.gmail.users()
                    .messages()
                    .get(
                        userId='me',
                        id=identifiers.message_id,
                        format='minimal',
                        fields='id,threadId',
                    )
                )
            case ResourceKind.GMAIL_THREAD:
                request = (
                    self._services.gmail.users()
                    .threads()
                    .get(
                        userId='me',
                        id=identifiers.thread_id,
                        format='minimal',
                        fields='id,messages(id)',
                    )
                )
            case ResourceKind.CALENDAR_EVENT:
                request = self._services.calendar.events().get(
                    calendarId=self._calendar_primary_id,
                    eventId=identifiers.event_id,
                    fields='id',
                )
            case ResourceKind.DRIVE_FOLDER | ResourceKind.DRIVE_FILE:
                request = self._services.drive.files().get(
                    fileId=identifiers.file_id,
                    fields='id',
                    supportsAllDrives=True,
                )
            case ResourceKind.SHEETS_SPREADSHEET:
                request = self._services.sheets.spreadsheets().get(
                    spreadsheetId=identifiers.spreadsheet_id,
                    fields='spreadsheetId',
                )
            case ResourceKind.SHEETS_TAB:
                request = self._services.sheets.spreadsheets().get(
                    spreadsheetId=identifiers.spreadsheet_id,
                    fields='sheets.properties.sheetId',
                )
            case ResourceKind.DOCS_DOCUMENT:
                request = self._services.docs.documents().get(
                    documentId=identifiers.document_id,
                    fields='documentId',
                )
            case ResourceKind.DOCS_TAB:
                request = self._services.docs.documents().get(
                    documentId=identifiers.document_id,
                    includeTabsContent=False,
                    fields='tabs(tabProperties(tabId),childTabs)',
                )
            case ResourceKind.GMAIL_DELIVERY:
                raise ValueError('Gmail delivery requires exact marker search')
        result = self._execute(request)
        if result is None:
            return False
        if binding.resource_kind is ResourceKind.SHEETS_TAB:
            return _contains_sheet_id(result, identifiers.sheet_id)
        if binding.resource_kind is ResourceKind.DOCS_TAB:
            return _contains_tab_id(result, identifiers.tab_id)
        return True


# === Readiness pass ===


def _contains_sheet_id(
    result: Mapping[str, Any],
    expected: int | None,
) -> bool:
    """Find one spreadsheet tab."""
    sheets = result.get('sheets') or ()
    if not isinstance(sheets, Sequence) or isinstance(sheets, str | bytes):
        raise ValueError('Sheets readiness response is invalid')
    return any(
        isinstance(sheet, Mapping)
        and isinstance(sheet.get('properties'), Mapping)
        and sheet['properties'].get('sheetId') == expected
        for sheet in sheets
    )


def _contains_tab_id(
    result: Mapping[str, Any],
    expected: str | None,
) -> bool:
    """Find one recursive document tab."""
    pending = list(result.get('tabs') or ())
    while pending:
        tab = pending.pop()
        if not isinstance(tab, Mapping):
            raise ValueError('Docs readiness response is invalid')
        properties = tab.get('tabProperties') or {}
        if not isinstance(properties, Mapping):
            raise ValueError('Docs readiness response is invalid')
        if properties.get('tabId') == expected:
            return True
        children = tab.get('childTabs') or ()
        if not isinstance(children, Sequence) or isinstance(
            children, str | bytes
        ):
            raise ValueError('Docs readiness response is invalid')
        pending.extend(children)
    return False


def _require_complete_bindings(bindings: FixtureBindings) -> None:
    """Require complete applied registry."""
    if frozenset(bindings.objects) != EXPECTED_LOGICAL_REFS:
        raise ValueError('bindings do not cover every logical ref')
    if bindings.applied_operations != frozenset(WRITE_OPERATION_IDS):
        raise ValueError('bindings do not cover every write operation')


def require_readiness_state(bindings: FixtureBindings) -> None:
    """Require readiness eligible state."""
    if bindings.state is BindingState.PLANNED:
        raise ValueError('planned bindings cannot be checked for readiness')


def check_readiness(
    bindings: FixtureBindings,
    probe: ReadinessProbe,
) -> ReadinessReport:
    """Run one bounded readiness pass."""
    require_readiness_state(bindings)
    missing = EXPECTED_LOGICAL_REFS - frozenset(bindings.objects)
    items: list[ReadinessItem] = []
    probe_count = 0
    for logical_ref in sorted(EXPECTED_LOGICAL_REFS):
        if logical_ref in missing:
            items.append(
                ReadinessItem(
                    logical_ref=logical_ref,
                    status='not_ready',
                    probe='missing_binding',
                )
            )
            continue
        binding = bindings.objects[logical_ref]
        fixture_object = OBJECTS_BY_REF[logical_ref]
        if (
            binding.service is not fixture_object.service
            or binding.resource_kind is not fixture_object.resource_kind
        ):
            raise ValueError('binding does not match the fixture catalog')
        if binding.resource_kind is ResourceKind.GMAIL_DELIVERY:
            marker = fixture_object.marker
            if marker is None:
                raise ValueError('Gmail delivery marker is unavailable')
            found = probe.find_gmail_delivery(
                exact_marker=marker,
                max_results=1,
            )
            probe_name = 'gmail_exact_marker_search'
        else:
            found = probe.object_exists(binding)
            probe_name = 'bound_object_read'
        probe_count += 1
        items.append(
            ReadinessItem(
                logical_ref=logical_ref,
                status='ready' if found else 'not_ready',
                probe=probe_name,
            )
        )
    status: Literal['ready', 'not_ready'] = (
        'ready'
        if items and all(item.status == 'ready' for item in items)
        else 'not_ready'
    )
    return ReadinessReport(
        fixture_version=FIXTURE_VERSION,
        status=status,
        probe_count=probe_count,
        items=tuple(items),
    )


def mark_bindings_ready(
    bindings: FixtureBindings,
    report: ReadinessReport,
) -> FixtureBindings:
    """Promote verified bindings state."""
    if report.fixture_version != bindings.fixture_version:
        raise ValueError('readiness version does not match bindings')
    if report.status != 'ready':
        raise ValueError('fixture is not ready')
    _require_complete_bindings(bindings)
    reported_refs = {item.logical_ref for item in report.items}
    if reported_refs != EXPECTED_LOGICAL_REFS:
        raise ValueError('readiness report does not cover every logical ref')
    if report.probe_count != len(EXPECTED_LOGICAL_REFS):
        raise ValueError('readiness report has an invalid probe count')
    return bindings.model_copy(update={'state': BindingState.READY})


def require_ready_for_xml(bindings: FixtureBindings) -> None:
    """Gate evaluation XML authoring."""
    if bindings.state is not BindingState.READY:
        raise ValueError('evaluation XML requires ready fixture bindings')
    _require_complete_bindings(bindings)
