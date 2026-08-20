"""Shared core test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Create private state directory."""
    path = tmp_path / 'state'
    path.mkdir(mode=0o700)
    return path
