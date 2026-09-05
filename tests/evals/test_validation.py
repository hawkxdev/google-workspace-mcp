"""Test evaluation catalog validation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from google_workspace_mcp.evals.normalizers import (
    AnswerNormalizationError,
    normalize_answer,
)
from google_workspace_mcp.evals.validation import (
    CatalogValidationError,
    load_evaluation_catalogs,
)

# === Normalizers ===


@pytest.mark.parametrize(
    ('normalizer', 'value', 'expected'),
    [
        ('exact_string', '  alpha value  ', 'alpha value'),
        ('integer', ' +0042 ', '42'),
        ('integer', '-0', '0'),
        ('decimal_1', '2.25', '2.3'),
        ('decimal_1', '-0.04', '-0.0'),
        ('boolean', ' TRUE ', 'true'),
        ('boolean', 'false', 'false'),
        ('date', '2028-02-29', '2028-02-29'),
        ('utc_datetime', '2027-02-16T09:00:00+03:00', '2027-02-16T06:00:00Z'),
        ('utc_datetime', '2027-02-16T06:00:00.000Z', '2027-02-16T06:00:00Z'),
    ],
)
def test_normalizers_return_canonical_values(
    normalizer: str,
    value: str,
    expected: str,
) -> None:
    assert normalize_answer(value, normalizer) == expected


@pytest.mark.parametrize(
    ('normalizer', 'value', 'message'),
    [
        ('exact_string', '   ', 'answer must be non-empty'),
        ('integer', '1.0', 'answer must be an integer'),
        ('integer', '1 2', 'answer must be an integer'),
        ('decimal_1', 'NaN', 'answer must be a finite decimal'),
        ('decimal_1', 'Infinity', 'answer must be a finite decimal'),
        ('boolean', 'yes', 'answer must be true or false'),
        ('date', '2027-02-29', 'answer must be an ISO date'),
        (
            'utc_datetime',
            '2027-02-16 06:00:00',
            'answer must be an ISO datetime',
        ),
        ('missing', 'value', 'unknown answer normalizer'),
    ],
)
def test_normalizers_reject_invalid_values(
    normalizer: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(AnswerNormalizationError, match=message):
        normalize_answer(value, normalizer)


def test_enum_normalizer_requires_one_closed_value() -> None:
    assert (
        normalize_answer('AMBER', 'enum', enum_values=('amber', 'blue'))
        == 'amber'
    )
    with pytest.raises(
        AnswerNormalizationError,
        match='answer is outside the enum',
    ):
        normalize_answer('green', 'enum', enum_values=('amber', 'blue'))


# === Catalog helpers ===


@pytest.fixture
def catalog_directory(tmp_path: Path) -> Path:
    """Copy public evaluation catalogs."""
    source = Path(__file__).parents[2] / 'evals'
    target = tmp_path / 'evals'
    target.mkdir()
    for xml_path in source.glob('*.xml'):
        shutil.copyfile(xml_path, target / xml_path.name)
    return target


def _replace_once(path: Path, old: str, new: str) -> None:
    """Replace one catalog fragment."""
    content = path.read_text(encoding='utf-8')
    assert content.count(old) == 1
    path.write_text(content.replace(old, new, 1), encoding='utf-8')


# === Catalog contract ===


def test_live_catalogs_load_with_fifty_pairs(catalog_directory: Path) -> None:
    catalogs = load_evaluation_catalogs(catalog_directory)

    assert [catalog.service.value for catalog in catalogs] == [
        'gmail',
        'calendar',
        'drive',
        'sheets',
        'docs',
    ]
    assert sum(len(catalog.pairs) for catalog in catalogs) == 50
    assert (
        len({pair.task_id for catalog in catalogs for pair in catalog.pairs})
        == 50
    )


def test_catalog_rejects_an_extra_pair(catalog_directory: Path) -> None:
    path = catalog_directory / 'drive.xml'
    content = path.read_text(encoding='utf-8')
    pair = content[
        content.index('  <qa_pair>') : content.index('  </qa_pair>') + 14
    ]
    path.write_text(
        content.replace('</evaluation>', f'{pair}\n</evaluation>'),
        encoding='utf-8',
    )

    with pytest.raises(
        CatalogValidationError, match='exactly ten qa_pair elements'
    ):
        load_evaluation_catalogs(catalog_directory)


def test_catalog_rejects_a_write_tool(catalog_directory: Path) -> None:
    _replace_once(
        catalog_directory / 'gmail.xml',
        '<allowed_tools><tool>gmail_get_draft</tool></allowed_tools>',
        '<allowed_tools><tool>gmail_send_message</tool></allowed_tools>',
    )

    with pytest.raises(
        CatalogValidationError, match='contains a forbidden tool'
    ):
        load_evaluation_catalogs(catalog_directory)


def test_catalog_rejects_a_foreign_logical_ref(
    catalog_directory: Path,
) -> None:
    _replace_once(
        catalog_directory / 'drive.xml',
        '<question>Call drive_list_folder with folder_id '
        'drive_fixture_folder, page_size 10, and no page_token. '
        'Return the integer file count.</question>\n'
        '    <expected_answer>2</expected_answer>\n'
        '    <normalizer>integer</normalizer>\n'
        '    <fixture_refs><ref>drive_fixture_folder</ref>'
        '<ref>drive_note_file</ref><ref>drive_ledger_file</ref>'
        '</fixture_refs>',
        '<question>Call drive_list_folder with folder_id '
        'drive_fixture_folder, page_size 10, and no page_token. '
        'Return the integer file count.</question>\n'
        '    <expected_answer>2</expected_answer>\n'
        '    <normalizer>integer</normalizer>\n'
        '    <fixture_refs><ref>drive_fixture_folder</ref>'
        '<ref>gmail_thread_alpha</ref><ref>drive_ledger_file</ref>'
        '</fixture_refs>',
    )

    with pytest.raises(
        CatalogValidationError, match='contains a foreign fixture ref'
    ):
        load_evaluation_catalogs(catalog_directory)


def test_catalog_rejects_a_second_normalizer(catalog_directory: Path) -> None:
    old = '\n'.join(
        (
            '<expected_answer>Synthetic document pearl-meadow-v8l3'
            '</expected_answer>',
            '    <normalizer>exact_string</normalizer>',
        )
    )
    new = old + '<normalizer>integer</normalizer>'
    _replace_once(
        catalog_directory / 'docs.xml',
        old,
        new,
    )

    with pytest.raises(
        CatalogValidationError, match='has unexpected elements'
    ):
        load_evaluation_catalogs(catalog_directory)


def test_catalog_rejects_zero_required_calls(catalog_directory: Path) -> None:
    old = '\n'.join(
        (
            '<allowed_tools><tool>calendar_search_events</tool>'
            '</allowed_tools>',
            '    <minimum_mcp_calls>1</minimum_mcp_calls>',
        )
    )
    new = old.replace('>1<', '>0<')
    _replace_once(
        catalog_directory / 'calendar.xml',
        old,
        new,
    )

    with pytest.raises(
        CatalogValidationError, match='invalid minimum_mcp_calls'
    ):
        load_evaluation_catalogs(catalog_directory)


def test_catalog_rejects_a_private_value(catalog_directory: Path) -> None:
    secret = 'private-control-value-7f95c6'
    question = (
        'Read sheets_primary metadata and return the spreadsheet title '
        'exactly.'
    )
    _replace_once(
        catalog_directory / 'sheets.xml',
        question,
        question.replace(' metadata', f' metadata {secret}'),
    )

    with pytest.raises(
        CatalogValidationError, match='contains a private value'
    ):
        load_evaluation_catalogs(
            catalog_directory,
            forbidden_values=(secret,),
        )


@pytest.mark.parametrize(
    ('encoded', 'forbidden_values'),
    [
        ('control&#64;example.test', ()),
        (
            'private-control-value-7f95c&#54;',
            ('private-control-value-7f95c6',),
        ),
        ('private&#47;evals', ()),
    ],
)
def test_catalog_rejects_entity_encoded_private_values(
    catalog_directory: Path,
    encoded: str,
    forbidden_values: tuple[str, ...],
) -> None:
    suffix = (
        'sheets_primary metadata and return the spreadsheet title exactly.'
    )
    _replace_once(
        catalog_directory / 'sheets.xml',
        f'Read {suffix}',
        f'Read {encoded} {suffix}',
    )

    with pytest.raises(
        CatalogValidationError,
        match='catalog contains a private value',
    ):
        load_evaluation_catalogs(
            catalog_directory,
            forbidden_values=forbidden_values,
        )


def test_catalog_rejects_entity_declarations(catalog_directory: Path) -> None:
    path = catalog_directory / 'gmail.xml'
    content = path.read_text(encoding='utf-8')
    declaration = (
        '<!DOCTYPE evaluation [<!ENTITY xxe SYSTEM '
        '"file:///etc/passwd">]>\n<evaluation '
    )
    path.write_text(
        content.replace(
            '<evaluation ',
            declaration,
            1,
        ),
        encoding='utf-8',
    )

    with pytest.raises(CatalogValidationError, match='unsafe XML declaration'):
        load_evaluation_catalogs(catalog_directory)


@pytest.mark.parametrize(
    'token',
    [
        'sk-ant-control-secret',
        'v1.control.identifier',
        'r1.control.identifier',
        '1//syntheticGoogleRefreshToken',
        'GOCSPX-syntheticGoogleClientSecret',
        'AIzaSyntheticGoogleApiKey1234567890',
    ],
)
def test_catalog_rejects_token_shapes(
    catalog_directory: Path,
    token: str,
) -> None:
    path = catalog_directory / 'docs.xml'
    content = path.read_text(encoding='utf-8')
    path.write_text(
        content.replace('Read docs_primary metadata', f'{token} metadata', 1),
        encoding='utf-8',
    )

    with pytest.raises(CatalogValidationError, match='private value'):
        load_evaluation_catalogs(catalog_directory)


def test_catalog_rejects_mixed_xml_text(catalog_directory: Path) -> None:
    path = catalog_directory / 'drive.xml'
    content = path.read_text(encoding='utf-8')
    path.write_text(
        content.replace('</task_id>', '</task_id>hidden content', 1),
        encoding='utf-8',
    )

    with pytest.raises(CatalogValidationError, match='unexpected elements'):
        load_evaluation_catalogs(catalog_directory)
