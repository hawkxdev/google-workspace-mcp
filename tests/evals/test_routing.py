"""Test evaluation route enforcement."""

from __future__ import annotations

from datetime import date

import pytest

from google_workspace_mcp.evals.models import FixtureBindings
from google_workspace_mcp.evals.normalizers import NormalizerName
from google_workspace_mcp.evals.routing import (
    RouteValidationError,
    project_tool_result,
    resolve_tool_arguments,
)
from google_workspace_mcp.evals.validation import EvaluationPair
from google_workspace_mcp.services.calendar.schemas import (
    CalendarListResponse,
    CalendarSummary,
    EventDate,
    EventDateTime,
    EventDetail,
)
from google_workspace_mcp.services.docs.schemas import (
    DocsContentResponse,
    DocsElementKind,
    DocsParagraphBlock,
    DocsTextElement,
)
from google_workspace_mcp.services.gmail.schemas import (
    DraftDetail,
    MessageDetail,
    ThreadDetail,
)

# === Helpers ===


def _pair(
    task_id: str,
    refs: tuple[str, ...],
    tools: tuple[str, ...],
) -> EvaluationPair:
    """Build one evaluation pair."""
    return EvaluationPair(
        task_id=task_id,
        question='Synthetic question',
        expected_answer='1',
        normalizer=NormalizerName.INTEGER,
        fixture_refs=refs,
        allowed_tools=tools,
        minimum_mcp_calls=1,
    )


# === Arguments ===


def test_message_logical_ref_resolves_to_private_id(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_04',
        ('gmail_message_alpha_root',),
        ('gmail_get_message',),
    )

    resolved = resolve_tool_arguments(
        pair,
        'gmail_get_message',
        {'message_id': 'gmail_message_alpha_root'},
        applied_bindings,
    )

    assert resolved == {
        'message_id': applied_bindings.objects[
            'gmail_message_alpha_root'
        ].identifiers.message_id
    }


def test_calendar_primary_alias_resolves_privately(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'calendar_01',
        ('calendar_event_timed',),
        ('calendar_search_events',),
    )

    resolved = resolve_tool_arguments(
        pair,
        'calendar_search_events',
        {
            'calendar_id': 'calendar_primary',
            'query': 'violet-ridge-h8p2',
            'time_min': '2027-02-01T00:00:00Z',
            'time_max': '2027-03-01T00:00:00Z',
        },
        applied_bindings,
    )

    assert resolved['calendar_id'] == (
        applied_bindings.calendar_primary_id.get_secret_value()
    )


def test_calendar_freebusy_requires_exact_fixture_window(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'calendar_09',
        ('calendar_event_timed',),
        ('calendar_get_freebusy',),
    )
    arguments = {
        'calendar_ids': ['calendar_primary'],
        'time_min': '2027-02-10T07:00:00Z',
        'time_max': '2027-02-10T07:45:00Z',
        'time_zone': 'UTC',
    }

    resolved = resolve_tool_arguments(
        pair,
        'calendar_get_freebusy',
        arguments,
        applied_bindings,
    )

    assert resolved['calendar_ids'] == [
        applied_bindings.calendar_primary_id.get_secret_value()
    ]
    with pytest.raises(
        RouteValidationError,
        match='free busy scope is outside the fixture',
    ):
        resolve_tool_arguments(
            pair,
            'calendar_get_freebusy',
            {**arguments, 'time_min': '2027-01-01T00:00:00Z'},
            applied_bindings,
        )


def test_route_rejects_a_disallowed_tool(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_04',
        ('gmail_message_alpha_root',),
        ('gmail_get_message',),
    )

    with pytest.raises(RouteValidationError, match='tool is not allowed'):
        resolve_tool_arguments(
            pair,
            'gmail_send_message',
            {},
            applied_bindings,
        )


def test_route_rejects_a_raw_provider_id(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_04',
        ('gmail_message_alpha_root',),
        ('gmail_get_message',),
    )
    private_id = applied_bindings.objects[
        'gmail_message_alpha_root'
    ].identifiers.message_id

    with pytest.raises(
        RouteValidationError, match='identifier must be a logical ref'
    ):
        resolve_tool_arguments(
            pair,
            'gmail_get_message',
            {'message_id': private_id},
            applied_bindings,
        )


def test_route_rejects_a_foreign_logical_ref(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'drive_05',
        ('drive_note_file',),
        ('drive_get_file',),
    )

    with pytest.raises(
        RouteValidationError, match='logical ref is outside the pair'
    ):
        resolve_tool_arguments(
            pair,
            'drive_get_file',
            {'file_id': 'drive_ledger_file'},
            applied_bindings,
        )


def test_docs_logical_refs_resolve_to_private_ids(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'docs_04',
        ('docs_primary', 'docs_primary_tab'),
        ('docs_read_content',),
    )

    resolved = resolve_tool_arguments(
        pair,
        'docs_read_content',
        {
            'document_id': 'docs_primary',
            'tab_id': 'docs_primary_tab',
            'max_chars': 200,
        },
        applied_bindings,
    )

    assert (
        resolved['document_id']
        == applied_bindings.objects['docs_primary'].identifiers.document_id
    )
    assert (
        resolved['tab_id']
        == applied_bindings.objects['docs_primary_tab'].identifiers.tab_id
    )


def test_route_rejects_shared_drive_and_foreign_page_tokens(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'drive_01',
        ('drive_ledger_file',),
        ('drive_search_files',),
    )

    for arguments in ({'drive_id': 'foreign'}, {'page_token': 'foreign'}):
        with pytest.raises(
            RouteValidationError,
            match='shared drive IDs are forbidden|page token was not issued',
        ):
            resolve_tool_arguments(
                pair,
                'drive_search_files',
                arguments,
                applied_bindings,
            )


def test_gmail_page_token_alias_round_trip(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_01',
        ('gmail_draft_cobalt',),
        ('gmail_list_drafts',),
    )

    projected = project_tool_result(
        pair,
        'gmail_list_drafts',
        {'items': [], 'next_page_token': 'private-page-token'},
        applied_bindings,
        page_token_aliases={'private-page-token': 'page_1'},
    )
    resolved = resolve_tool_arguments(
        pair,
        'gmail_list_drafts',
        {'page_size': 20, 'page_token': 'page_1'},
        applied_bindings,
        allowed_page_tokens={'page_1': 'private-page-token'},
    )

    assert projected == {'items': [], 'next_page_token': 'page_1'}
    assert resolved == {
        'page_size': 20,
        'page_token': 'private-page-token',
    }


def test_drive_search_requires_marker_or_bound_parent(
    applied_bindings: FixtureBindings,
) -> None:
    exact_pair = _pair(
        'drive_01',
        ('drive_fixture_folder',),
        ('drive_search_files',),
    )
    parent_pair = _pair(
        'drive_09',
        ('drive_fixture_folder', 'drive_note_file'),
        ('drive_search_files',),
    )

    with pytest.raises(
        RouteValidationError,
        match='Drive search must use a fixture boundary',
    ):
        resolve_tool_arguments(
            exact_pair,
            'drive_search_files',
            {'exact_name': 'Private report'},
            applied_bindings,
        )
    exact = resolve_tool_arguments(
        exact_pair,
        'drive_search_files',
        {'exact_name': 'Synthetic fixture crimson-grove-f6j1'},
        applied_bindings,
    )
    parent = resolve_tool_arguments(
        parent_pair,
        'drive_search_files',
        {'parent_id': 'drive_fixture_folder', 'mime_types': ['text/plain']},
        applied_bindings,
    )

    assert exact['exact_name'] == 'Synthetic fixture crimson-grove-f6j1'
    assert (
        parent['parent_id']
        == applied_bindings.objects['drive_fixture_folder'].identifiers.file_id
    )


# === Projections ===


def test_gmail_projection_removes_addresses_and_urls(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_04',
        ('gmail_message_alpha_root',),
        ('gmail_get_message',),
    )
    private_id = applied_bindings.objects[
        'gmail_message_alpha_root'
    ].identifiers.message_id

    projected = project_tool_result(
        pair,
        'gmail_get_message',
        {
            'message_id': private_id,
            'thread_id': 'foreign-thread-id',
            'subject': 'Synthetic subject',
            'body_text': 'Root message marker: amber-lake-q4n8-root-f3.',
            'sender': 'private-owner@example.com',
            'recipients': ['private-owner@example.com'],
            'html_link': 'https://mail.google.test/private',
        },
        applied_bindings,
    )

    assert projected == {
        'message_id': 'gmail_message_alpha_root',
        'subject': 'Synthetic subject',
        'body_text': 'Root message marker: amber-lake-q4n8-root-f3.',
    }


def test_gmail_labels_keep_only_sorted_system_labels(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_10',
        ('gmail_delivery_beta_root',),
        ('gmail_list_labels',),
    )

    projected = project_tool_result(
        pair,
        'gmail_list_labels',
        {
            'items': [
                {'label_id': 'SENT', 'name': 'SENT', 'label_type': 'system'},
                {
                    'label_id': 'private',
                    'name': 'private',
                    'label_type': 'user',
                },
                {'label_id': 'INBOX', 'name': 'INBOX', 'label_type': 'system'},
            ]
        },
        applied_bindings,
    )

    assert projected == {
        'items': [
            {'label_id': 'INBOX', 'name': 'INBOX', 'label_type': 'system'},
            {'label_id': 'SENT', 'name': 'SENT', 'label_type': 'system'},
        ]
    }


def test_search_projection_drops_unbound_results(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'drive_10',
        ('drive_ledger_file',),
        ('drive_search_files',),
    )
    fixture_id = applied_bindings.objects[
        'drive_ledger_file'
    ].identifiers.file_id

    projected = project_tool_result(
        pair,
        'drive_search_files',
        {
            'files': [
                {
                    'file_id': fixture_id,
                    'name': 'Synthetic ledger teal-harbor-n5s8.csv',
                    'mime_type': 'text/csv',
                },
                {
                    'file_id': 'foreign-file-id',
                    'name': 'Private file',
                    'mime_type': 'text/plain',
                },
            ]
        },
        applied_bindings,
    )

    assert projected == {
        'files': [
            {
                'file_id': 'drive_ledger_file',
                'name': 'Synthetic ledger teal-harbor-n5s8.csv',
                'mime_type': 'text/csv',
            }
        ]
    }


def test_draft_projection_preserves_real_message_shape(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_02',
        ('gmail_draft_cobalt', 'gmail_draft_message_cobalt'),
        ('gmail_get_draft',),
    )
    draft = DraftDetail(
        draft_id=applied_bindings.objects[
            'gmail_draft_cobalt'
        ].identifiers.draft_id,
        message=MessageDetail(
            message_id=applied_bindings.objects[
                'gmail_draft_message_cobalt'
            ].identifiers.message_id,
            thread_id='',
            subject='Synthetic draft cobalt-pine-k7v3',
            sender='private-owner@example.test',
            body_text='Private evaluation draft marker: cobalt-pine-k7v3.',
        ),
    )

    projected = project_tool_result(
        pair,
        'gmail_get_draft',
        draft.model_dump(mode='json'),
        applied_bindings,
    )

    assert projected['message']['subject'] == (
        'Synthetic draft cobalt-pine-k7v3'
    )
    assert projected['message']['body_text'] == (
        'Private evaluation draft marker: cobalt-pine-k7v3.'
    )
    assert 'sender' not in projected['message']


def test_thread_projection_preserves_real_message_collection(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'gmail_07',
        (
            'gmail_thread_alpha',
            'gmail_message_alpha_root',
            'gmail_message_alpha_reply',
        ),
        ('gmail_get_thread',),
    )
    thread_id = applied_bindings.objects[
        'gmail_thread_alpha'
    ].identifiers.thread_id
    message_ids = [
        applied_bindings.objects[logical_ref].identifiers.message_id
        for logical_ref in pair.fixture_refs[1:]
    ]
    thread = ThreadDetail(
        thread_id=thread_id,
        messages=tuple(
            MessageDetail(
                message_id=message_id,
                thread_id=thread_id,
                subject='Synthetic conversation amber-lake-q4n8',
            )
            for message_id in message_ids
        ),
    )

    projected = project_tool_result(
        pair,
        'gmail_get_thread',
        thread.model_dump(mode='json'),
        applied_bindings,
    )

    assert len(projected['messages']) == 2


def test_calendar_projection_preserves_real_time_shapes(
    applied_bindings: FixtureBindings,
) -> None:
    timed_pair = _pair(
        'calendar_03',
        ('calendar_event_timed',),
        ('calendar_get_event',),
    )
    all_day_pair = _pair(
        'calendar_04',
        ('calendar_event_all_day',),
        ('calendar_get_event',),
    )
    calendar_id = applied_bindings.calendar_primary_id.get_secret_value()
    timed = EventDetail(
        event_id=applied_bindings.objects[
            'calendar_event_timed'
        ].identifiers.event_id,
        calendar_id=calendar_id,
        start=EventDateTime(
            date_time='2027-02-10T10:00:00+03:00',
            time_zone='Europe/Minsk',
        ),
        end=EventDateTime(
            date_time='2027-02-10T10:45:00+03:00',
            time_zone='Europe/Minsk',
        ),
    )
    all_day = EventDetail(
        event_id=applied_bindings.objects[
            'calendar_event_all_day'
        ].identifiers.event_id,
        calendar_id=calendar_id,
        start=EventDate(date=date(2027, 2, 12)),
        end=EventDate(date=date(2027, 2, 14)),
    )

    timed_projection = project_tool_result(
        timed_pair,
        'calendar_get_event',
        timed.model_dump(mode='json'),
        applied_bindings,
    )
    all_day_projection = project_tool_result(
        all_day_pair,
        'calendar_get_event',
        all_day.model_dump(mode='json'),
        applied_bindings,
    )

    assert timed_projection['start']['date_time'] == (
        '2027-02-10T10:00:00+03:00'
    )
    assert all_day_projection['start']['date'] == '2027-02-12'


def test_calendar_list_projection_drops_private_summary(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'calendar_10',
        ('calendar_event_timed',),
        ('calendar_list_calendars',),
    )
    calendar_id = applied_bindings.calendar_primary_id.get_secret_value()
    response = CalendarListResponse(
        items=(
            CalendarSummary(
                calendar_id=calendar_id,
                summary='private-owner@example.test',
                time_zone='Europe/Minsk',
                primary=True,
                access_role='owner',
            ),
        )
    )

    projected = project_tool_result(
        pair,
        'calendar_list_calendars',
        response.model_dump(mode='json'),
        applied_bindings,
    )

    assert projected == {
        'items': [
            {
                'calendar_id': 'calendar_primary',
                'primary': True,
                'time_zone': 'Europe/Minsk',
                'access_role': 'owner',
            }
        ]
    }


def test_calendar_freebusy_projection_exposes_only_occupancy(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'calendar_09',
        ('calendar_event_timed',),
        ('calendar_get_freebusy',),
    )

    projected = project_tool_result(
        pair,
        'calendar_get_freebusy',
        {
            'time_min': '2027-02-10T07:00:00Z',
            'time_max': '2027-02-10T07:45:00Z',
            'calendars': [
                {
                    'calendar_id': 'private-calendar',
                    'busy': [
                        {
                            'start': '2027-02-10T07:00:00.000Z',
                            'end': '2027-02-10T08:45:00+01:00',
                        }
                    ],
                }
            ],
        },
        applied_bindings,
    )

    assert projected == {'busy': True}


def test_docs_projection_preserves_real_elements(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'docs_05',
        ('docs_primary', 'docs_primary_tab'),
        ('docs_read_content',),
    )
    response = DocsContentResponse(
        document_id=applied_bindings.objects[
            'docs_primary'
        ].identifiers.document_id,
        revision_id='private-revision',
        tab_id=applied_bindings.objects['docs_primary_tab'].identifiers.tab_id,
        start_index=1,
        end_index=20,
        blocks=(
            DocsParagraphBlock(
                start_index=1,
                end_index=20,
                named_style='NORMAL_TEXT',
                elements=(
                    DocsTextElement(
                        kind=DocsElementKind.TEXT_RUN,
                        start_index=1,
                        end_index=20,
                        content='Synthetic marker pearl-meadow-v8l3',
                    ),
                ),
            ),
        ),
    )

    projected = project_tool_result(
        pair,
        'docs_read_content',
        response.model_dump(mode='json'),
        applied_bindings,
    )

    assert projected['blocks'][0]['elements'][0]['content'] == (
        'Synthetic marker pearl-meadow-v8l3'
    )
    assert 'revision_id' not in projected


def test_projection_rejects_non_structured_content(
    applied_bindings: FixtureBindings,
) -> None:
    pair = _pair(
        'drive_10',
        ('drive_ledger_file',),
        ('drive_search_files',),
    )

    with pytest.raises(
        RouteValidationError, match='structured content is required'
    ):
        project_tool_result(
            pair,
            'drive_search_files',
            ['not', 'an', 'object'],
            applied_bindings,
        )
