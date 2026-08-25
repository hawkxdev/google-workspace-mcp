"""Test managed Gmail attachments."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from google_workspace_mcp.services.gmail.attachments import (
    ManagedAttachmentStore,
)
from google_workspace_mcp.services.gmail.errors import GmailAttachmentError


def _encoded(data: bytes) -> str:
    """Encode attachment data."""
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def test_managed_store_sanitizes_name_and_sets_mode(tmp_path: Path) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    stored = ManagedAttachmentStore(directory, max_bytes=100).save(
        'message-1',
        'attachment-1',
        '../invoice.pdf',
        4,
        _encoded(b'data'),
    )
    path = Path(stored.path)
    assert path.parent == directory
    assert path.name == 'message-1_attachment-1_invoice.pdf'
    assert path.read_bytes() == b'data'
    assert path.stat().st_mode & 0o777 == 0o600


def test_managed_store_rejects_size_collision_and_symlink(
    tmp_path: Path,
) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    store = ManagedAttachmentStore(directory, max_bytes=3)
    with pytest.raises(GmailAttachmentError, match='attachment is too large'):
        store.save('m', 'a', 'x.bin', 4, _encoded(b'data'))

    safe_store = ManagedAttachmentStore(directory, max_bytes=100)
    safe_store.save('m', 'a', 'x.bin', 1, _encoded(b'x'))
    with pytest.raises(
        GmailAttachmentError, match='attachment already exists'
    ):
        safe_store.save('m', 'a', 'x.bin', 1, _encoded(b'x'))

    real = tmp_path / 'real'
    real.mkdir(mode=0o700)
    symlink = tmp_path / 'linked'
    symlink.symlink_to(real, target_is_directory=True)
    with pytest.raises(
        GmailAttachmentError, match='download directory is unsafe'
    ):
        ManagedAttachmentStore(symlink).save(
            'm', 'a', 'x.bin', 1, _encoded(b'x')
        )


def test_managed_store_completes_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    original_write = os.write

    def partial_write(fd: int, data: bytes | memoryview) -> int:
        """Write one byte only."""
        return original_write(fd, bytes(data[:1]))

    monkeypatch.setattr(os, 'write', partial_write)
    stored = ManagedAttachmentStore(directory, max_bytes=100).save(
        'm', 'a', 'x.bin', 4, _encoded(b'data')
    )
    assert Path(stored.path).read_bytes() == b'data'


def test_managed_store_rejects_symlink_ancestor(tmp_path: Path) -> None:
    real = tmp_path / 'real'
    downloads = real / 'downloads'
    downloads.mkdir(parents=True, mode=0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(
        GmailAttachmentError, match='download directory is unsafe'
    ):
        ManagedAttachmentStore(linked / 'downloads').save(
            'm', 'a', 'x.bin', 1, _encoded(b'x')
        )


def test_managed_store_cleans_zero_progress_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(os, 'write', lambda _fd, _data: 0)
    with pytest.raises(GmailAttachmentError, match='attachment write failed'):
        ManagedAttachmentStore(directory).save(
            'm', 'a', 'x.bin', 1, _encoded(b'x')
        )
    assert list(directory.iterdir()) == []


def test_managed_store_removes_publish_on_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    original_fsync = os.fsync
    calls = 0

    def failing_directory_fsync(fd: int) -> None:
        """Fail the second durability barrier."""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('directory fsync failed')
        original_fsync(fd)

    monkeypatch.setattr(os, 'fsync', failing_directory_fsync)
    with pytest.raises(GmailAttachmentError, match='attachment write failed'):
        ManagedAttachmentStore(directory).save(
            'm', 'a', 'x.bin', 1, _encoded(b'x')
        )
    assert list(directory.iterdir()) == []


def test_managed_store_bounds_generated_filename(tmp_path: Path) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    stored = ManagedAttachmentStore(directory).save(
        'm' * 300,
        'a' * 300,
        ('file' * 100) + '.txt',
        1,
        _encoded(b'x'),
    )
    assert len(Path(stored.path).name.encode()) <= 240


def test_managed_store_rejects_zero_size_mismatch(tmp_path: Path) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    with pytest.raises(
        GmailAttachmentError, match='attachment size is invalid'
    ):
        ManagedAttachmentStore(directory).save(
            'm', 'a', 'x.bin', 0, _encoded(b'x')
        )
    assert list(directory.iterdir()) == []


def test_managed_store_rejects_large_encoding_before_decode(
    tmp_path: Path,
) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    with pytest.raises(GmailAttachmentError, match='attachment is too large'):
        ManagedAttachmentStore(directory, max_bytes=10).save(
            'm', 'a', 'x.bin', 1, 'eA' * 10_000
        )


def test_managed_store_rejects_noncanonical_padding(tmp_path: Path) -> None:
    directory = tmp_path / 'downloads'
    directory.mkdir(mode=0o700)
    store = ManagedAttachmentStore(directory, max_bytes=10)
    with pytest.raises(GmailAttachmentError, match='encoding is invalid'):
        store.save('m', 'a', 'x.bin', 1, 'eA=')
    with pytest.raises(GmailAttachmentError, match='attachment is too large'):
        store.save('m', 'a', 'x.bin', 1, 'eA' + ('=' * 100))
