"""Run evaluation fixture commands."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .apply import apply_fixture
from .models import (
    FIXTURE_VERSION,
    ApplicationConfirmation,
    load_bindings,
)
from .preview import build_preview


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
        default=Path('private/google-tokens'),
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
    return parser


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
