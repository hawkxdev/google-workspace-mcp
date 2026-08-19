"""Общие фикстуры тестов ядра."""

from pathlib import Path

import pytest


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Приватный каталог состояния с правами 0700."""
    path = tmp_path / 'state'
    path.mkdir(mode=0o700)
    return path
