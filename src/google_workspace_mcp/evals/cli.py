"""Run evaluation fixture commands."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .apply import (
    apply_fixture,
    build_application_services,
    save_bindings,
)
from .models import (
    FIXTURE_VERSION,
    ApplicationConfirmation,
    load_bindings,
)
from .preview import build_preview
from .readiness import (
    FixtureReadinessError,
    GoogleReadinessProbe,
    check_readiness,
    mark_bindings_ready,
    require_readiness_state,
)


def _parser() -> argparse.ArgumentParser:
    """Build fixture command parser."""
    parser = argparse.ArgumentParser(
        prog='python -m google_workspace_mcp.evals'
    )
    subcommands = parser.add_subparsers(dest='command', required=True)
    preview = subcommands.add_parser('preview')
    preview.add_argument('--bindings', type=Path)
    apply = subcommands.add_parser('apply')
    apply.add_argument('--bindings', type=Path, required=True)
    apply.add_argument(
        '--credentials-dir',
        type=Path,
        required=True,
    )
    apply.add_argument(
        '--fixture-version',
        choices=(FIXTURE_VERSION,),
        required=True,
    )
    apply.add_argument('--preview-digest', required=True)
    apply.add_argument(
        '--acknowledge-writes',
        action='store_true',
        required=True,
    )
    readiness = subcommands.add_parser('readiness')
    readiness.add_argument('--bindings', type=Path, required=True)
    readiness.add_argument(
        '--credentials-dir',
        type=Path,
        required=True,
    )
    return parser


def _run_apply(arguments: argparse.Namespace) -> int:
    """Run confirmed fixture writes."""
    confirmation = ApplicationConfirmation(
        fixture_version=arguments.fixture_version,
        preview_digest=arguments.preview_digest,
        acknowledge_writes=arguments.acknowledge_writes,
    )
    result = apply_fixture(
        arguments.bindings,
        confirmation,
        credentials_dir=arguments.credentials_dir,
    )
    print(
        json.dumps(
            {
                'fixture_version': result.fixture_version,
                'state': result.state.value,
                'applied_operation_count': len(result.applied_operations),
            },
            sort_keys=True,
        )
    )
    return 0


def _run_readiness(arguments: argparse.Namespace) -> int:
    """Run live readiness checks."""
    bindings = load_bindings(arguments.bindings)
    require_readiness_state(bindings)
    calendar_primary = bindings.calendar_primary_id
    if calendar_primary is None:
        raise ValueError('calendar_primary_id is required for readiness')
    calendar_primary_id = calendar_primary.get_secret_value()
    if not calendar_primary_id:
        raise ValueError('calendar_primary_id is required for readiness')
    try:
        services = build_application_services(arguments.credentials_dir)
        probe = GoogleReadinessProbe(
            services,
            calendar_primary_id=calendar_primary_id,
        )
        report = check_readiness(bindings, probe)
    except Exception:
        raise FixtureReadinessError('fixture readiness check failed') from None
    result = bindings
    if report.status == 'ready':
        result = mark_bindings_ready(bindings, report)
        save_bindings(arguments.bindings, result)
    ready_count = sum(item.status == 'ready' for item in report.items)
    print(
        json.dumps(
            {
                'binding_state': result.state.value,
                'fixture_version': report.fixture_version,
                'not_ready_count': len(report.items) - ready_count,
                'probe_count': report.probe_count,
                'readiness_status': report.status,
                'ready_count': ready_count,
            },
            sort_keys=True,
        )
    )
    return 0 if report.status == 'ready' else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one fixture command."""
    arguments = _parser().parse_args(argv)
    if arguments.command == 'preview':
        bindings = (
            load_bindings(arguments.bindings)
            if arguments.bindings is not None
            else None
        )
        preview = build_preview(bindings)
        print(preview.document.model_dump_json(indent=2))
        return 0
    if arguments.command == 'apply':
        return _run_apply(arguments)
    return _run_readiness(arguments)
