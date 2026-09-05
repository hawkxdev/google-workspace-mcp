"""Test canonical run state."""

from __future__ import annotations

from pathlib import Path

import pytest

from google_workspace_mcp.evals.run_state import claim_canonical_run


def test_run_claim_is_private_and_exclusive(tmp_path: Path) -> None:
    directory = tmp_path / 'oauth'
    directory.mkdir(mode=0o700)

    path = claim_canonical_run(directory, 'stage12-v1')

    assert path.stat().st_mode & 0o777 == 0o600
    assert 'stage12-v1' in path.read_text(encoding='utf-8')
    with pytest.raises(
        ValueError,
        match='canonical evaluation run is already claimed',
    ):
        claim_canonical_run(directory, 'stage12-v1')


def test_run_claim_rejects_open_directory(tmp_path: Path) -> None:
    directory = tmp_path / 'oauth'
    directory.mkdir(mode=0o755)

    with pytest.raises(
        ValueError,
        match='run state directory mode must be 0700',
    ):
        claim_canonical_run(directory, 'stage12-v1')
