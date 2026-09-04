"""Build write requests safely."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build

from .catalog import (
    MARKER_CALENDAR_ALL_DAY,
    MARKER_CALENDAR_RECURRING,
    MARKER_CALENDAR_TIMED,
    MARKER_DOCS,
    MARKER_DRAFT,
    MARKER_DRIVE_FOLDER,
    MARKER_DRIVE_LEDGER,
    MARKER_DRIVE_NOTE,
    MARKER_MESSAGE_ALPHA_REPLY,
    MARKER_MESSAGE_ALPHA_ROOT,
    MARKER_MESSAGE_BETA_ROOT,
    MARKER_SHEETS,
    MARKER_THREAD_ALPHA,
    MARKER_THREAD_BETA,
    OWNER_EMAIL_REFERENCE,
    PRIMARY_CALENDAR_REFERENCE,
)
from .models import PreviewOperation, ServiceName

# === Request contracts ===


class RequestLike(Protocol):
    """Expose inspectable Google request."""

    uri: str
    method: str
    body: str | bytes | None
    body_size: int

    def execute(self, **kwargs: Any) -> Any:
        """Execute one provider request."""
        ...


@dataclass(frozen=True, slots=True)
class GoogleServiceSet:
    """Hold five discovery clients."""

    gmail: Any
    calendar: Any
    drive: Any
    sheets: Any
    docs: Any


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """Pair request with preview."""

    request: RequestLike
    preview: PreviewOperation


ServiceFactory = Callable[[], GoogleServiceSet]

WRITE_OPERATION_IDS = (
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


def build_preview_services() -> GoogleServiceSet:
    """Build anonymous discovery clients."""
    credentials = AnonymousCredentials()
    common = {
        'credentials': credentials,
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


# === MIME builders ===


def _plain_message(
    subject: str,
    body: str,
    *,
    thread_ref: str | None = None,
    header_ref: str | None = None,
    message_header: str | None = None,
) -> str:
    """Build synthetic MIME payload."""
    message = EmailMessage()
    message['To'] = 'fixture-owner@example.com'
    message['Subject'] = subject
    if message_header is not None:
        message['Message-ID'] = f'<{message_header}>'
    if header_ref is not None:
        message['In-Reply-To'] = f'<{header_ref}>'
        message['References'] = f'<{header_ref}>'
    if thread_ref is not None:
        message['X-Fixture-Thread-Reference'] = thread_ref
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')


def _safe_mail_body(
    subject: str,
    body: str,
    *,
    thread_ref: str | None = None,
    header_ref: str | None = None,
) -> dict[str, Any]:
    """Render redacted MIME details."""
    output: dict[str, Any] = {
        'to': {'private_reference': OWNER_EMAIL_REFERENCE},
        'subject': subject,
        'text': body,
    }
    if thread_ref is not None:
        output['thread_id'] = {'logical_reference': thread_ref}
    if header_ref is not None:
        output['message_header_id'] = {'logical_reference': header_ref}
    return output


# === Preview helpers ===


def _operation(
    request: RequestLike,
    *,
    operation_id: str,
    service: ServiceName,
    action: str,
    outputs: tuple[str, ...],
    body: dict[str, Any],
    depends_on: tuple[str, ...] = (),
    private_references: tuple[str, ...] = (),
) -> PreparedOperation:
    """Create one prepared operation."""
    return PreparedOperation(
        request=request,
        preview=PreviewOperation(
            operation_id=operation_id,
            service=service,
            action=action,
            status='planned',
            logical_outputs=outputs,
            depends_on=depends_on,
            private_references=private_references,
            method=request.method,
            uri=request.uri,
            body=body,
        ),
    )


def _json_body(request: RequestLike) -> dict[str, Any]:
    """Decode one JSON request body."""
    raw = request.body
    if raw is None:
        return {}
    text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Google request body must be an object')
    return value


# === Service operations ===


def _gmail_operations(service: Any) -> tuple[PreparedOperation, ...]:
    """Build Gmail write requests."""
    users = service.users()
    draft_subject = f'Synthetic draft {MARKER_DRAFT}'
    draft_text = f'Private evaluation draft marker: {MARKER_DRAFT}.'
    draft_request = users.drafts().create(
        userId='me',
        body={
            'message': {
                'raw': _plain_message(draft_subject, draft_text),
            }
        },
    )
    alpha_subject = f'Synthetic conversation {MARKER_THREAD_ALPHA}'
    alpha_root_text = f'Root message marker: {MARKER_MESSAGE_ALPHA_ROOT}.'
    alpha_root = users.messages().send(
        userId='me',
        body={
            'raw': _plain_message(
                alpha_subject,
                alpha_root_text,
                message_header=(
                    f'{MARKER_MESSAGE_ALPHA_ROOT}@example.invalid'
                ),
            ),
        },
    )
    alpha_reply_text = f'Reply message marker: {MARKER_MESSAGE_ALPHA_REPLY}.'
    alpha_reply = users.messages().send(
        userId='me',
        body={
            'raw': _plain_message(
                f'Re: {alpha_subject}',
                alpha_reply_text,
                thread_ref='gmail_thread_alpha',
                header_ref=(f'{MARKER_MESSAGE_ALPHA_ROOT}@example.invalid'),
            ),
            'threadId': '__binding_gmail_thread_alpha__',
        },
    )
    beta_subject = f'Synthetic notice {MARKER_THREAD_BETA}'
    beta_text = f'Single-message thread marker: {MARKER_MESSAGE_BETA_ROOT}.'
    beta_root = users.messages().send(
        userId='me',
        body={
            'raw': _plain_message(
                beta_subject,
                beta_text,
                message_header=f'{MARKER_MESSAGE_BETA_ROOT}@example.invalid',
            ),
        },
    )
    return (
        _operation(
            draft_request,
            operation_id='gmail.create_draft.cobalt',
            service=ServiceName.GMAIL,
            action='create_draft',
            outputs=(
                'gmail_draft_cobalt',
                'gmail_draft_message_cobalt',
            ),
            private_references=(OWNER_EMAIL_REFERENCE,),
            body={
                'message': _safe_mail_body(draft_subject, draft_text),
            },
        ),
        _operation(
            alpha_root,
            operation_id='gmail.send_message.alpha_root',
            service=ServiceName.GMAIL,
            action='send_message',
            outputs=(
                'gmail_message_alpha_root',
                'gmail_thread_alpha',
                'gmail_delivery_alpha_root',
            ),
            private_references=(OWNER_EMAIL_REFERENCE,),
            body=_safe_mail_body(alpha_subject, alpha_root_text),
        ),
        _operation(
            alpha_reply,
            operation_id='gmail.send_message.alpha_reply',
            service=ServiceName.GMAIL,
            action='send_message',
            outputs=(
                'gmail_message_alpha_reply',
                'gmail_delivery_alpha_reply',
            ),
            depends_on=(
                'gmail_message_alpha_root',
                'gmail_thread_alpha',
            ),
            private_references=(OWNER_EMAIL_REFERENCE,),
            body=_safe_mail_body(
                f'Re: {alpha_subject}',
                alpha_reply_text,
                thread_ref='gmail_thread_alpha',
                header_ref='gmail_message_alpha_root.message_header_id',
            ),
        ),
        _operation(
            beta_root,
            operation_id='gmail.send_message.beta_root',
            service=ServiceName.GMAIL,
            action='send_message',
            outputs=(
                'gmail_message_beta_root',
                'gmail_thread_beta',
                'gmail_delivery_beta_root',
            ),
            private_references=(OWNER_EMAIL_REFERENCE,),
            body=_safe_mail_body(beta_subject, beta_text),
        ),
    )


def _calendar_operations(service: Any) -> tuple[PreparedOperation, ...]:
    """Build Calendar write requests."""
    events = service.events()
    bodies = (
        {
            'summary': f'Synthetic timed event {MARKER_CALENDAR_TIMED}',
            'description': MARKER_CALENDAR_TIMED,
            'start': {
                'dateTime': '2027-02-10T10:00:00+03:00',
                'timeZone': 'Europe/Minsk',
            },
            'end': {
                'dateTime': '2027-02-10T10:45:00+03:00',
                'timeZone': 'Europe/Minsk',
            },
        },
        {
            'summary': f'Synthetic all-day event {MARKER_CALENDAR_ALL_DAY}',
            'description': MARKER_CALENDAR_ALL_DAY,
            'start': {'date': '2027-02-12'},
            'end': {'date': '2027-02-14'},
        },
        {
            'summary': (
                f'Synthetic recurring event {MARKER_CALENDAR_RECURRING}'
            ),
            'description': MARKER_CALENDAR_RECURRING,
            'start': {
                'dateTime': '2027-02-16T09:00:00+03:00',
                'timeZone': 'Europe/Minsk',
            },
            'end': {
                'dateTime': '2027-02-16T09:30:00+03:00',
                'timeZone': 'Europe/Minsk',
            },
            'recurrence': ['RRULE:FREQ=DAILY;COUNT=3'],
        },
    )
    names = ('timed', 'all_day', 'recurring')
    return tuple(
        _operation(
            events.insert(
                calendarId='__binding_calendar_primary_id__',
                body=body,
                sendUpdates='none',
            ),
            operation_id=f'calendar.create_event.{name}',
            service=ServiceName.CALENDAR,
            action='create_event',
            outputs=(f'calendar_event_{name}',),
            private_references=(PRIMARY_CALENDAR_REFERENCE,),
            body=body,
        )
        for name, body in zip(names, bodies, strict=True)
    )


def _drive_operations(service: Any) -> tuple[PreparedOperation, ...]:
    """Build Drive write requests."""
    files = service.files()
    folder_body = {
        'name': f'Synthetic fixture {MARKER_DRIVE_FOLDER}',
        'mimeType': 'application/vnd.google-apps.folder',
        'description': MARKER_DRIVE_FOLDER,
        'appProperties': {'fixture_marker': MARKER_DRIVE_FOLDER},
    }
    note_body = {
        'name': f'Synthetic note {MARKER_DRIVE_NOTE}.txt',
        'mimeType': 'text/plain',
        'description': MARKER_DRIVE_NOTE,
        'parents': ['__binding_drive_fixture_folder__'],
        'appProperties': {'fixture_marker': MARKER_DRIVE_NOTE},
    }
    ledger_body = {
        'name': f'Synthetic ledger {MARKER_DRIVE_LEDGER}.csv',
        'mimeType': 'text/csv',
        'description': MARKER_DRIVE_LEDGER,
        'parents': ['__binding_drive_fixture_folder__'],
        'appProperties': {'fixture_marker': MARKER_DRIVE_LEDGER},
    }
    definitions = (
        ('folder', folder_body, 'drive_fixture_folder', ()),
        (
            'note',
            note_body,
            'drive_note_file',
            ('drive_fixture_folder',),
        ),
        (
            'ledger',
            ledger_body,
            'drive_ledger_file',
            ('drive_fixture_folder',),
        ),
    )
    return tuple(
        _operation(
            files.create(
                body=body,
                supportsAllDrives=True,
                fields='id,name,mimeType,parents,version',
            ),
            operation_id=f'drive.create.{name}',
            service=ServiceName.DRIVE,
            action='create_folder' if name == 'folder' else 'create_file',
            outputs=(logical_ref,),
            depends_on=depends_on,
            body=body,
        )
        for name, body, logical_ref, depends_on in definitions
    )


def _sheets_operations(service: Any) -> tuple[PreparedOperation, ...]:
    """Build Sheets write requests."""
    spreadsheets = service.spreadsheets()
    create_body = {
        'properties': {
            'title': f'Synthetic workbook {MARKER_SHEETS}',
            'locale': 'en_US',
            'timeZone': 'Europe/Minsk',
        },
        'sheets': [
            {
                'properties': {
                    'sheetId': 41001,
                    'title': 'Inputs',
                    'gridProperties': {
                        'rowCount': 20,
                        'columnCount': 6,
                    },
                }
            },
            {
                'properties': {
                    'sheetId': 41002,
                    'title': 'Summary',
                    'gridProperties': {
                        'rowCount': 20,
                        'columnCount': 6,
                    },
                }
            },
        ],
    }
    values_body = {
        'valueInputOption': 'USER_ENTERED',
        'data': [
            {
                'range': 'Inputs!A1:C4',
                'majorDimension': 'ROWS',
                'values': [
                    ['marker', 'category', 'amount'],
                    [MARKER_SHEETS, 'cobalt', 12.5],
                    [MARKER_SHEETS, 'amber', 7.5],
                    [MARKER_SHEETS, 'silver', 5],
                ],
            },
            {
                'range': 'Summary!A1:B3',
                'majorDimension': 'ROWS',
                'values': [
                    ['metric', 'value'],
                    ['row_count', '=COUNTA(Inputs!B2:B4)'],
                    ['amount_total', '=SUM(Inputs!C2:C4)'],
                ],
            },
        ],
    }
    create_request = spreadsheets.create(body=create_body)
    values_request = spreadsheets.values().batchUpdate(
        spreadsheetId='__binding_sheets_primary__',
        body=values_body,
    )
    return (
        _operation(
            create_request,
            operation_id='sheets.create.primary',
            service=ServiceName.SHEETS,
            action='create_spreadsheet',
            outputs=(
                'sheets_primary',
                'sheets_inputs_tab',
                'sheets_summary_tab',
            ),
            body=_json_body(create_request),
        ),
        _operation(
            values_request,
            operation_id='sheets.write.primary_values',
            service=ServiceName.SHEETS,
            action='batch_update_values',
            outputs=(),
            depends_on=('sheets_primary',),
            body=_json_body(values_request),
        ),
    )


def _docs_operations(service: Any) -> tuple[PreparedOperation, ...]:
    """Build Docs write requests."""
    documents = service.documents()
    create_body = {'title': f'Synthetic document {MARKER_DOCS}'}
    text = (
        f'Synthetic document marker: {MARKER_DOCS}.\n'
        'Cobalt items: 3.\nAmber total: 25.\n'
    )
    update_body = {
        'requests': [
            {
                'insertText': {
                    'text': text,
                    'location': {
                        'index': 1,
                        'tabId': '__binding_docs_primary_tab__',
                    },
                }
            }
        ]
    }
    create_request = documents.create(body=create_body)
    update_request = documents.batchUpdate(
        documentId='__binding_docs_primary__',
        body=update_body,
    )
    return (
        _operation(
            create_request,
            operation_id='docs.create.primary',
            service=ServiceName.DOCS,
            action='create_document',
            outputs=('docs_primary', 'docs_primary_tab'),
            body=_json_body(create_request),
        ),
        _operation(
            update_request,
            operation_id='docs.write.primary_text',
            service=ServiceName.DOCS,
            action='batch_update_document',
            outputs=(),
            depends_on=('docs_primary', 'docs_primary_tab'),
            body=_json_body(update_request),
        ),
    )


def build_write_operations(
    services: GoogleServiceSet,
) -> tuple[PreparedOperation, ...]:
    """Build every future write request."""
    operations = (
        *_gmail_operations(services.gmail),
        *_calendar_operations(services.calendar),
        *_drive_operations(services.drive),
        *_sheets_operations(services.sheets),
        *_docs_operations(services.docs),
    )
    built_ids = tuple(item.preview.operation_id for item in operations)
    if built_ids != WRITE_OPERATION_IDS:
        raise ValueError('write operation registry is inconsistent')
    return operations
