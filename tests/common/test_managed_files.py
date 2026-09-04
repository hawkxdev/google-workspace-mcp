"""Managed file store tests."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from google_workspace_mcp.common.managed_files import (
    ManagedFileError,
    ManagedFileStore,
)


def test_managed_store_publishes_bytes_atomically(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=100)
    record = store.publish_bytes(
        'gmail',
        'attachment-1',
        'report.txt',
        'text/plain',
        5,
        b'hello',
    )
    target = tmp_path / record.managed_name
    assert target.read_bytes() == b'hello'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert record.size == 5
    assert record.sha256 == hashlib.sha256(b'hello').hexdigest()


def test_managed_writer_hashes_chunked_content(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=1000)
    content = b'chunk1_data_' + b'chunk2_data_' + b'chunk3_data'
    with store.writer(
        'drive',
        'file-1',
        'data.bin',
        'application/octet-stream',
        expected_size=len(content),
    ) as writer:
        writer.write(b'chunk1_data_')
        assert (
            writer.current_sha256
            == hashlib.sha256(b'chunk1_data_').hexdigest()
        )
        writer.write(b'chunk2_data_')
        writer.write(b'chunk3_data')
        assert writer.current_sha256 == hashlib.sha256(content).hexdigest()
        record = writer.commit()

    assert record.size == len(content)
    assert record.sha256 == hashlib.sha256(content).hexdigest()
    assert (tmp_path / record.managed_name).read_bytes() == content

    with store.open_verified(
        record.managed_name,
        expected_size=record.size,
        expected_sha256=record.sha256,
    ) as file_obj:
        assert file_obj.read() == content


def test_managed_store_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=100)
    for invalid in (
        '../etc/passwd',
        '/etc/passwd',
        'a/b',
        'a\\b',
        '\x00',
        '.',
        '..',
        '',
    ):
        with (
            pytest.raises(
                ManagedFileError, match='managed file name is invalid'
            ),
            store.open_verified(invalid, 1, 'dummy'),
        ):
            pass

    real = tmp_path / 'real'
    real.mkdir(mode=0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ManagedFileError, match='download directory is unsafe'):
        ManagedFileStore(linked).publish_bytes(
            'ns', 'obj', 'f.txt', 'text/plain', 4, b'test'
        )

    downloads = real / 'downloads'
    downloads.mkdir(mode=0o700)
    with pytest.raises(ManagedFileError, match='download directory is unsafe'):
        ManagedFileStore(linked / 'downloads').publish_bytes(
            'ns', 'obj', 'f.txt', 'text/plain', 4, b'test'
        )

    target_file = tmp_path / 'target.txt'
    target_file.write_bytes(b'data')
    sym_file = tmp_path / 'sym_file.txt'
    sym_file.symlink_to(target_file)
    with (
        pytest.raises(ManagedFileError),
        store.open_verified(
            'sym_file.txt', 4, hashlib.sha256(b'data').hexdigest()
        ),
    ):
        pass


def test_managed_store_rejects_size_and_digest_mismatch(
    tmp_path: Path,
) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=10)
    with pytest.raises(ManagedFileError, match='too large'):
        store.publish_bytes('ns', 'obj', 'f.txt', 'text/plain', 20, b'x' * 20)
    assert not any(tmp_path.iterdir())

    with pytest.raises(ManagedFileError, match='size mismatch'):
        store.publish_bytes('ns', 'obj', 'f.txt', 'text/plain', 5, b'hello_1')
    assert not any(tmp_path.iterdir())

    record = store.publish_bytes(
        'ns', 'obj', 'f.txt', 'text/plain', 4, b'data'
    )
    with (
        pytest.raises(ManagedFileError, match='size mismatch'),
        store.open_verified(record.managed_name, 5, record.sha256),
    ):
        pass

    with (
        pytest.raises(ManagedFileError, match='digest mismatch'),
        store.open_verified(record.managed_name, record.size, '0' * 64),
    ):
        pass


def test_managed_store_cleans_failed_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=100)

    monkeypatch.setattr(os, 'write', lambda _fd, _data: 0)
    with pytest.raises(ManagedFileError, match='write failed'):
        store.publish_bytes('ns', 'obj', 'f.txt', 'text/plain', 4, b'data')
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()

    original_fsync = os.fsync
    calls = 0

    def failing_fsync(fd: int) -> None:
        """Simulate fsync failure."""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('directory fsync failed')
        original_fsync(fd)

    monkeypatch.setattr(os, 'fsync', failing_fsync)
    with pytest.raises(ManagedFileError, match='write failed'):
        store.publish_bytes('ns', 'obj', 'f.txt', 'text/plain', 4, b'data')
    assert list(tmp_path.iterdir()) == []


def test_managed_store_rejects_hardlink_and_nonregular_file(
    tmp_path: Path,
) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=100)
    record = store.publish_bytes(
        'ns', 'obj', 'f.txt', 'text/plain', 4, b'data'
    )

    hardlink_path = tmp_path / 'hardlink.txt'
    os.link(tmp_path / record.managed_name, hardlink_path)
    with (
        pytest.raises(ManagedFileError, match='multiple links|not regular'),
        store.open_verified('hardlink.txt', record.size, record.sha256),
    ):
        pass

    sub_dir = tmp_path / 'subdir'
    sub_dir.mkdir(mode=0o700)
    with (
        pytest.raises(ManagedFileError, match='not regular'),
        store.open_verified('subdir', 0, '0' * 64),
    ):
        pass


def test_managed_store_bounds_collision_names(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_bytes=100)
    record1 = store.publish_bytes(
        'ns', 'obj', 'report.txt', 'text/plain', 5, b'data1'
    )
    record2 = store.publish_bytes(
        'ns', 'obj', 'report.txt', 'text/plain', 5, b'data2'
    )
    record3 = store.publish_bytes(
        'ns', 'obj', 'report.txt', 'text/plain', 5, b'data3'
    )

    assert record1.managed_name != record2.managed_name
    assert record2.managed_name != record3.managed_name
    assert (tmp_path / record1.managed_name).read_bytes() == b'data1'
    assert (tmp_path / record2.managed_name).read_bytes() == b'data2'
    assert (tmp_path / record3.managed_name).read_bytes() == b'data3'

    long_record = store.publish_bytes(
        'n' * 300,
        'o' * 300,
        ('file' * 100) + '.txt',
        'text/plain',
        4,
        b'long',
    )
    assert len(long_record.managed_name.encode()) <= 255
    assert (tmp_path / long_record.managed_name).read_bytes() == b'long'
