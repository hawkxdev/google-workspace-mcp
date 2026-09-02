"""Run evaluation fixture preview."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .models import load_bindings
from .preview import build_preview


def _parser() -> argparse.ArgumentParser:
    """Build preview argument parser."""
    parser = argparse.ArgumentParser(
        prog='python -m google_workspace_mcp.evals'
    )
    subcommands = parser.add_subparsers(dest='command', required=True)
    preview = subcommands.add_parser('preview')
    preview.add_argument('--bindings', type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print complete write preview."""
    arguments = _parser().parse_args(argv)
    bindings = (
        load_bindings(arguments.bindings)
        if arguments.bindings is not None
        else None
    )
    preview = build_preview(bindings)
    print(preview.document.model_dump_json(indent=2))
    return 0
