"""Test evaluation XML catalog."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from google_workspace_mcp.evals.catalog import (
    EXPECTED_LOGICAL_REFS,
    FIXTURE_OBJECTS,
    OBJECTS_BY_REF,
)

# === Catalog contract ===

EVALS_DIRECTORY = Path(__file__).parents[2] / 'evals'
SERVICES = ('gmail', 'calendar', 'drive', 'sheets', 'docs')
PAIR_ELEMENTS = (
    'task_id',
    'question',
    'expected_answer',
    'normalizer',
    'fixture_refs',
    'allowed_tools',
    'minimum_mcp_calls',
)
NORMALIZERS = frozenset(
    {
        'exact_string',
        'integer',
        'decimal_1',
        'boolean',
        'date',
        'utc_datetime',
        'enum',
    }
)
READONLY_TOOLS = {
    'gmail': frozenset(
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
    'calendar': frozenset(
        {
            'calendar_list_calendars',
            'calendar_search_events',
            'calendar_get_event',
            'calendar_list_event_instances',
            'calendar_get_freebusy',
        }
    ),
    'drive': frozenset(
        {
            'drive_search_files',
            'drive_get_file',
            'drive_list_folder',
        }
    ),
    'sheets': frozenset(
        {
            'sheets_get_spreadsheet',
            'sheets_read_range',
            'sheets_batch_read_ranges',
        }
    ),
    'docs': frozenset(
        {
            'docs_get_document',
            'docs_read_content',
        }
    ),
}
PRIVATE_MARKERS = (
    '@',
    'bindings.owner_email',
    'bindings.calendar_primary_id',
    '__binding_',
    'private/',
    'google-tokens',
    'oauth/',
)


# === Parsing ===


def _parse_service(service: str) -> tuple[Path, ET.Element]:
    """Parse one service catalog."""
    xml_path = EVALS_DIRECTORY / f'{service}.xml'
    return xml_path, ET.parse(xml_path).getroot()  # noqa: S314


# === Contract tests ===


@pytest.mark.parametrize('service', SERVICES)
def test_service_catalog_has_exact_shape(service: str) -> None:
    xml_path, root = _parse_service(service)

    assert root.tag == 'evaluation'
    assert root.attrib == {
        'fixture_version': 'stage12-v1',
        'service': service,
    }
    pairs = root.findall('./qa_pair')
    assert len(pairs) == 10
    assert list(root) == pairs
    assert [pair.findtext('task_id') for pair in pairs] == [
        f'{service}_{index:02d}' for index in range(1, 11)
    ]
    assert xml_path.read_bytes().startswith(b'<?xml version="1.0"')


@pytest.mark.parametrize('service', SERVICES)
def test_service_pairs_use_public_readonly_contract(service: str) -> None:
    xml_path, root = _parse_service(service)

    for pair in root.findall('./qa_pair'):
        assert tuple(child.tag for child in pair) == PAIR_ELEMENTS
        task_id = pair.findtext('task_id')
        question = pair.findtext('question')
        expected_answer = pair.findtext('expected_answer')
        normalizer = pair.findtext('normalizer')
        fixture_refs_element = pair.find('fixture_refs')
        allowed_tools_element = pair.find('allowed_tools')
        fixture_refs = [
            item.text for item in pair.findall('./fixture_refs/ref')
        ]
        allowed_tools = [
            item.text for item in pair.findall('./allowed_tools/tool')
        ]
        minimum_calls = pair.findtext('minimum_mcp_calls')

        assert task_id
        assert question
        assert expected_answer
        assert normalizer in NORMALIZERS
        assert fixture_refs_element is not None
        assert fixture_refs
        assert len(fixture_refs) == len(set(fixture_refs))
        assert all(item.tag == 'ref' for item in fixture_refs_element)
        assert set(fixture_refs) <= EXPECTED_LOGICAL_REFS
        assert all(
            OBJECTS_BY_REF[logical_ref].service.value == service
            for logical_ref in fixture_refs
        )
        assert allowed_tools_element is not None
        assert allowed_tools
        assert len(allowed_tools) == len(set(allowed_tools))
        assert all(item.tag == 'tool' for item in allowed_tools_element)
        assert set(allowed_tools) <= READONLY_TOOLS[service]
        assert minimum_calls is not None
        assert len(allowed_tools) <= int(minimum_calls) <= 12
        if normalizer == 'boolean':
            assert expected_answer in {'true', 'false'}
        elif normalizer == 'integer':
            assert re.fullmatch(r'-?[0-9]+', expected_answer)
        elif normalizer == 'decimal_1':
            assert re.fullmatch(r'-?[0-9]+\.[0-9]', expected_answer)
        elif normalizer == 'date':
            assert re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', expected_answer)
        elif normalizer == 'utc_datetime':
            assert re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:'
                r'[0-9]{2}:[0-9]{2}Z',
                expected_answer,
            )

    public_text = xml_path.read_text(encoding='utf-8')
    assert all(marker not in public_text for marker in PRIVATE_MARKERS)
    assert re.search(r'[А-Яа-яЁё]', public_text) is None


def test_catalog_has_fifty_unique_tasks() -> None:
    task_ids = []
    questions = []
    for service in SERVICES:
        _, root = _parse_service(service)
        for pair in root.findall('./qa_pair'):
            task_ids.append(pair.findtext('task_id'))
            questions.append(pair.findtext('question'))

    assert len(task_ids) == 50
    assert len(set(task_ids)) == 50
    assert len(questions) == 50
    assert len(set(questions)) == 50


def test_catalog_covers_fixture_objects_and_tools() -> None:
    fixture_refs = set()
    for service in SERVICES:
        _, root = _parse_service(service)
        pairs = root.findall('./qa_pair')
        fixture_refs.update(
            item.text for item in root.findall('./qa_pair/fixture_refs/ref')
        )
        tools = {
            item.text for item in root.findall('./qa_pair/allowed_tools/tool')
        }

        assert tools == READONLY_TOOLS[service]
        assert len({pair.findtext('question') for pair in pairs}) == 10

    assert fixture_refs == EXPECTED_LOGICAL_REFS


def test_catalog_uses_every_public_marker() -> None:
    public_text = ''.join(
        (EVALS_DIRECTORY / f'{service}.xml').read_text(encoding='utf-8')
        for service in SERVICES
    )
    expected_markers = {
        fixture.marker
        for fixture in FIXTURE_OBJECTS
        if fixture.marker is not None
    }

    assert all(marker in public_text for marker in expected_markers)
