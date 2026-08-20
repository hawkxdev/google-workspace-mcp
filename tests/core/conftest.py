"""Shared core test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Create a private state directory with mode 0700."""
    path = tmp_path / 'state'
    path.mkdir(mode=0o700)
    return path
