"""Define synthetic fixture catalog."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ResourceKind, ServiceName

# === Public markers ===

OWNER_EMAIL_REFERENCE = 'bindings.owner_email'
PRIMARY_CALENDAR_REFERENCE = 'bindings.calendar_primary_id'
MARKER_DRAFT = 'cobalt-pine-k7v3'
MARKER_THREAD_ALPHA = 'amber-lake-q4n8'
MARKER_MESSAGE_ALPHA_ROOT = 'amber-lake-q4n8-root-f3'
MARKER_MESSAGE_ALPHA_REPLY = 'amber-lake-q4n8-reply-z9'
MARKER_THREAD_BETA = 'silver-fern-m2x6'
MARKER_MESSAGE_BETA_ROOT = 'silver-fern-m2x6-root-c5'
MARKER_CALENDAR_TIMED = 'violet-ridge-h8p2'
MARKER_CALENDAR_ALL_DAY = 'bronze-field-t5c9'
MARKER_CALENDAR_RECURRING = 'indigo-brook-r3w7'
MARKER_DRIVE_FOLDER = 'crimson-grove-f6j1'
MARKER_DRIVE_NOTE = 'saffron-delta-b9k4'
MARKER_DRIVE_LEDGER = 'teal-harbor-n5s8'
MARKER_SHEETS = 'umber-orbit-d2g7'
MARKER_DOCS = 'pearl-meadow-v8l3'


@dataclass(frozen=True, slots=True)
class FixtureObject:
    """Describe one synthetic object."""

    logical_ref: str
    service: ServiceName
    resource_kind: ResourceKind
    marker: str | None = None


FIXTURE_OBJECTS = (
    FixtureObject(
        'gmail_draft_cobalt',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_DRAFT,
        MARKER_DRAFT,
    ),
    FixtureObject(
        'gmail_draft_message_cobalt',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_MESSAGE,
        MARKER_DRAFT,
    ),
    FixtureObject(
        'gmail_message_alpha_root',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_MESSAGE,
        MARKER_MESSAGE_ALPHA_ROOT,
    ),
    FixtureObject(
        'gmail_message_alpha_reply',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_MESSAGE,
        MARKER_MESSAGE_ALPHA_REPLY,
    ),
    FixtureObject(
        'gmail_thread_alpha',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_THREAD,
        MARKER_THREAD_ALPHA,
    ),
    FixtureObject(
        'gmail_delivery_alpha_root',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_DELIVERY,
        MARKER_MESSAGE_ALPHA_ROOT,
    ),
    FixtureObject(
        'gmail_delivery_alpha_reply',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_DELIVERY,
        MARKER_MESSAGE_ALPHA_REPLY,
    ),
    FixtureObject(
        'gmail_message_beta_root',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_MESSAGE,
        MARKER_MESSAGE_BETA_ROOT,
    ),
    FixtureObject(
        'gmail_thread_beta',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_THREAD,
        MARKER_THREAD_BETA,
    ),
    FixtureObject(
        'gmail_delivery_beta_root',
        ServiceName.GMAIL,
        ResourceKind.GMAIL_DELIVERY,
        MARKER_MESSAGE_BETA_ROOT,
    ),
    FixtureObject(
        'calendar_event_timed',
        ServiceName.CALENDAR,
        ResourceKind.CALENDAR_EVENT,
        MARKER_CALENDAR_TIMED,
    ),
    FixtureObject(
        'calendar_event_all_day',
        ServiceName.CALENDAR,
        ResourceKind.CALENDAR_EVENT,
        MARKER_CALENDAR_ALL_DAY,
    ),
    FixtureObject(
        'calendar_event_recurring',
        ServiceName.CALENDAR,
        ResourceKind.CALENDAR_EVENT,
        MARKER_CALENDAR_RECURRING,
    ),
    FixtureObject(
        'drive_fixture_folder',
        ServiceName.DRIVE,
        ResourceKind.DRIVE_FOLDER,
        MARKER_DRIVE_FOLDER,
    ),
    FixtureObject(
        'drive_note_file',
        ServiceName.DRIVE,
        ResourceKind.DRIVE_FILE,
        MARKER_DRIVE_NOTE,
    ),
    FixtureObject(
        'drive_ledger_file',
        ServiceName.DRIVE,
        ResourceKind.DRIVE_FILE,
        MARKER_DRIVE_LEDGER,
    ),
    FixtureObject(
        'sheets_primary',
        ServiceName.SHEETS,
        ResourceKind.SHEETS_SPREADSHEET,
        MARKER_SHEETS,
    ),
    FixtureObject(
        'sheets_inputs_tab',
        ServiceName.SHEETS,
        ResourceKind.SHEETS_TAB,
        MARKER_SHEETS,
    ),
    FixtureObject(
        'sheets_summary_tab',
        ServiceName.SHEETS,
        ResourceKind.SHEETS_TAB,
        MARKER_SHEETS,
    ),
    FixtureObject(
        'docs_primary',
        ServiceName.DOCS,
        ResourceKind.DOCS_DOCUMENT,
        MARKER_DOCS,
    ),
    FixtureObject(
        'docs_primary_tab',
        ServiceName.DOCS,
        ResourceKind.DOCS_TAB,
        MARKER_DOCS,
    ),
)

OBJECTS_BY_REF = {item.logical_ref: item for item in FIXTURE_OBJECTS}
EXPECTED_LOGICAL_REFS = frozenset(OBJECTS_BY_REF)
