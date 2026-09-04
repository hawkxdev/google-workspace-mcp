"""Test private binding validation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from google_workspace_mcp.evals.models import (
    BindingState,
    FixtureBindings,
    load_bindings,
)

from .conftest import make_bindings


def _json_payload(bindings: FixtureBindings) -> dict[str, object]:
    """Build serializable private payload."""
    payload = bindings.model_dump(mode='json')
    payload['owner_email'] = 'fixture-owner@example.com'
    payload['calendar_primary_id'] = 'primary-calendar-private-value'
    return payload


def test_load_bindings_accepts_protected_valid_file(
    protected_json_file: Path,
) -> None:
    source = make_bindings(state=BindingState.APPLIED)
    protected_json_file.write_text(
        json.dumps(_json_payload(source)),
        encoding='utf-8',
    )

    loaded = load_bindings(protected_json_file)

    assert loaded.fixture_version == 'stage12-v1'
    assert loaded.state is BindingState.APPLIED
    assert loaded.owner_email is not None
    assert loaded.owner_email.get_secret_value() == (
        'fixture-owner@example.com'
    )


def test_load_bindings_rejects_group_readable_file(
    protected_json_file: Path,
) -> None:
    protected_json_file.write_text('{}', encoding='utf-8')
    protected_json_file.chmod(0o640)

    with pytest.raises(ValueError, match='mode must be 0600'):
        load_bindings(protected_json_file)


def test_load_bindings_rejects_symbolic_link(
    protected_json_file: Path,
) -> None:
    source = make_bindings()
    protected_json_file.write_text(
        json.dumps(_json_payload(source)),
        encoding='utf-8',
    )
    link = protected_json_file.with_name('bindings-link.json')
    link.symlink_to(protected_json_file)

    with pytest.raises(ValueError, match='unavailable'):
        load_bindings(link)


def test_bindings_reject_unknown_fields() -> None:
    payload = _json_payload(make_bindings())
    payload['access_token'] = 'not-allowed'

    with pytest.raises(ValidationError, match='Extra inputs'):
        FixtureBindings.model_validate(payload)


def test_secret_fields_do_not_serialize_values() -> None:
    bindings = make_bindings(owner_email='owner@confidential.invalid')

    serialized = bindings.model_dump_json()

    assert 'owner@confidential.invalid' not in serialized
    assert 'primary-calendar-private-value' not in serialized
    assert '**********' in serialized


def test_binding_loader_keeps_file_descriptor_closed(
    protected_json_file: Path,
) -> None:
    protected_json_file.write_text('{', encoding='utf-8')

    with pytest.raises(ValueError, match='valid JSON'):
        load_bindings(protected_json_file)

    os.rename(protected_json_file, protected_json_file.with_suffix('.moved'))
