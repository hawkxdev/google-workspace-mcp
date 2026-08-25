"""Manage service local files."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')
_MAX_NAME_BYTES = 255


class ManagedFileError(Exception):
    """Report managed file failure."""


@dataclass(frozen=True, slots=True)
class ManagedFileRecord:
    """Describe published managed file."""

    managed_name: str
    original_name: str
    mime_type: str
    size: int
    sha256: str


def _validate_managed_name(value: str) -> str:
    """Validate managed file basename."""
    if (
        not value
        or value in {'.', '..'}
        or '/' in value
        or '\\' in value
        or '\x00' in value
    ):
        raise ManagedFileError('managed file name is invalid')
    return value


def _safe_component(value: str, fallback: str) -> str:
    """Sanitize managed filename component."""
    sanitized = _SAFE_NAME.sub('_', value).strip('._')
    return sanitized or fallback


class ManagedFileWriter:
    """Write managed file chunks."""

    def __init__(
        self,
        store: ManagedFileStore,
        namespace: str,
        object_id: str,
        original_name: str,
        mime_type: str,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Initialize managed file writer."""
        self._store = store
        self._namespace = namespace
        self._object_id = object_id
        self._original_name = original_name
        self._mime_type = mime_type
        self._expected_size = expected_size

        effective_limit = store.max_bytes
        if max_bytes is not None:
            effective_limit = min(effective_limit, max_bytes)
        self._effective_max_bytes = effective_limit

        if expected_size is not None:
            if expected_size < 0:
                raise ManagedFileError('expected size must be non-negative')
            if expected_size > self._effective_max_bytes:
                raise ManagedFileError('managed file is too large')

        self._directory_fd: int | None = store._open_directory()
        self._temp_name = f'.tmp_{secrets.token_hex(12)}'
        self._temp_fd: int | None = None
        self._linked_name: str | None = None
        self._published = False
        self._closed = False
        self._hasher = hashlib.sha256()
        self._bytes_written = 0

        try:
            self._temp_fd = os.open(
                self._temp_name,
                _CREATE_FLAGS,
                0o600,
                dir_fd=self._directory_fd,
            )
            os.fchmod(self._temp_fd, 0o600)
        except Exception:
            self._cleanup_resources()
            raise

    @property
    def current_sha256(self) -> str:
        """Return current content digest."""
        return self._hasher.hexdigest()

    def write(self, data: bytes) -> int:
        """Write binary file bytes."""
        if self._closed or self._published:
            raise ManagedFileError('managed file writer is closed')
        if self._temp_fd is None:
            raise ManagedFileError('managed file write failed')

        if self._bytes_written + len(data) > self._effective_max_bytes:
            raise ManagedFileError('managed file is too large')
        if (
            self._expected_size is not None
            and self._bytes_written + len(data) > self._expected_size
        ):
            raise ManagedFileError('managed file size mismatch')

        remaining = memoryview(data)
        while remaining:
            try:
                written = os.write(self._temp_fd, remaining)
            except OSError as exc:
                raise ManagedFileError('managed file write failed') from exc
            if written <= 0:
                raise ManagedFileError('managed file write failed')
            self._hasher.update(remaining[:written])
            self._bytes_written += written
            remaining = remaining[written:]

        return len(data)

    def _candidate_names(self) -> Iterator[str]:
        """Generate candidate target filenames."""
        safe_ns = _safe_component(self._namespace, 'namespace')[:40]
        safe_obj = _safe_component(self._object_id, 'object')[:40]
        provider_name = Path(self._original_name).name
        safe_file = _safe_component(provider_name, 'file.bin')
        suffix = Path(safe_file).suffix[:20]
        stem = Path(safe_file).stem

        # Base candidate
        stem_limit = 140 - len(suffix)
        safe_stem = stem[:stem_limit]
        base_name = f'{safe_ns}_{safe_obj}_{safe_stem}{suffix}'
        if len(base_name.encode()) > _MAX_NAME_BYTES:
            overflow = len(base_name.encode()) - _MAX_NAME_BYTES
            safe_stem = safe_stem[:-overflow]
            base_name = f'{safe_ns}_{safe_obj}_{safe_stem}{suffix}'
        yield base_name

        # Collision candidates
        for counter in range(1, 1000):
            counter_tag = f'_{counter}'
            limit = 140 - len(suffix) - len(counter_tag)
            c_stem = stem[:limit]
            c_name = f'{safe_ns}_{safe_obj}_{c_stem}{counter_tag}{suffix}'
            if len(c_name.encode()) > _MAX_NAME_BYTES:
                overflow = len(c_name.encode()) - _MAX_NAME_BYTES
                c_stem = c_stem[:-overflow]
                c_name = f'{safe_ns}_{safe_obj}_{c_stem}{counter_tag}{suffix}'
            yield c_name

    def commit(self) -> ManagedFileRecord:
        """Publish managed file atomically."""
        if self._closed or self._published:
            raise ManagedFileError('managed file writer is closed')
        if self._temp_fd is None or self._directory_fd is None:
            raise ManagedFileError('managed file write failed')

        if (
            self._expected_size is not None
            and self._bytes_written != self._expected_size
        ):
            raise ManagedFileError('managed file size mismatch')

        try:
            os.fsync(self._temp_fd)
            os.close(self._temp_fd)
            self._temp_fd = None

            published_name: str | None = None
            for candidate in self._candidate_names():
                try:
                    os.link(
                        self._temp_name,
                        candidate,
                        src_dir_fd=self._directory_fd,
                        dst_dir_fd=self._directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                published_name = candidate
                break

            if published_name is None:
                raise ManagedFileError('managed file collision limit exceeded')

            self._linked_name = published_name
            os.unlink(self._temp_name, dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
            self._published = True

            return ManagedFileRecord(
                managed_name=published_name,
                original_name=self._original_name,
                mime_type=self._mime_type,
                size=self._bytes_written,
                sha256=self._hasher.hexdigest(),
            )
        except ManagedFileError:
            raise
        except OSError as exc:
            raise ManagedFileError('managed file write failed') from exc
        finally:
            self._cleanup_resources()

    def _cleanup_resources(self) -> None:
        """Clean temporary file resources."""
        if self._temp_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._temp_fd)
            self._temp_fd = None

        if self._directory_fd is not None:
            if not self._published:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.unlink(self._temp_name, dir_fd=self._directory_fd)
                if self._linked_name is not None:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        os.unlink(self._linked_name, dir_fd=self._directory_fd)
                    with contextlib.suppress(OSError):
                        os.fsync(self._directory_fd)
            with contextlib.suppress(OSError):
                os.close(self._directory_fd)
            self._directory_fd = None

        self._closed = True

    def __enter__(self) -> ManagedFileWriter:
        """Enter writer context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit writer resource context."""
        self._cleanup_resources()


class ManagedFileStore:
    """Store managed service files."""

    def __init__(
        self,
        directory: Path,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        """Initialize managed file store."""
        self._directory = Path(os.path.abspath(directory))
        self._max_bytes = max_bytes

    @property
    def directory(self) -> Path:
        """Return root directory path."""
        return self._directory

    @property
    def max_bytes(self) -> int:
        """Return maximum size limit."""
        return self._max_bytes

    def _open_directory(self) -> int:
        """Open root directory descriptor."""
        descriptor = os.open('/', _DIRECTORY_FLAGS)
        try:
            for part in self._directory.parts[1:]:
                next_descriptor = os.open(
                    part,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError:
            os.close(descriptor)
            raise ManagedFileError('download directory is unsafe') from None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise ManagedFileError('download directory is unsafe')
        return descriptor

    def publish_bytes(
        self,
        namespace: str,
        object_id: str,
        original_name: str,
        mime_type: str,
        expected_size: int,
        data: bytes,
    ) -> ManagedFileRecord:
        """Publish byte payload atomically."""
        if expected_size < 0:
            raise ManagedFileError('expected size must be non-negative')
        if expected_size > self._max_bytes or len(data) > self._max_bytes:
            raise ManagedFileError('managed file is too large')
        if len(data) != expected_size:
            raise ManagedFileError('managed file size mismatch')

        with self.writer(
            namespace=namespace,
            object_id=object_id,
            original_name=original_name,
            mime_type=mime_type,
            expected_size=expected_size,
            max_bytes=self._max_bytes,
        ) as writer:
            writer.write(data)
            return writer.commit()

    def writer(
        self,
        namespace: str,
        object_id: str,
        original_name: str,
        mime_type: str,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> ManagedFileWriter:
        """Create managed file writer."""
        return ManagedFileWriter(
            self,
            namespace=namespace,
            object_id=object_id,
            original_name=original_name,
            mime_type=mime_type,
            expected_size=expected_size,
            max_bytes=max_bytes,
        )

    @contextmanager
    def open_verified(
        self,
        managed_name: str,
        expected_size: int,
        expected_sha256: str,
    ) -> Iterator[BinaryIO]:
        """Open verified managed file."""
        _validate_managed_name(managed_name)
        if expected_size < 0:
            raise ManagedFileError('expected size must be non-negative')

        dir_fd = self._open_directory()
        file_fd: int | None = None
        file_obj: BinaryIO | None = None
        try:
            try:
                file_fd = os.open(
                    managed_name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                raise ManagedFileError(
                    'managed file not found or inaccessible'
                ) from exc

            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ManagedFileError('managed file is not regular')
            if metadata.st_nlink != 1:
                raise ManagedFileError('managed file has multiple links')
            if metadata.st_size != expected_size:
                raise ManagedFileError('managed file size mismatch')

            file_obj = os.fdopen(file_fd, 'rb', closefd=True)
            file_fd = None

            hasher = hashlib.sha256()
            bytes_read = 0
            while chunk := file_obj.read(1024 * 1024):
                hasher.update(chunk)
                bytes_read += len(chunk)

            if bytes_read != expected_size:
                raise ManagedFileError('managed file size mismatch')
            if hasher.hexdigest().lower() != expected_sha256.lower():
                raise ManagedFileError('managed file digest mismatch')

            file_obj.seek(0)
            yield file_obj
        finally:
            if file_obj is not None:
                with contextlib.suppress(OSError):
                    file_obj.close()
            elif file_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(file_fd)
            with contextlib.suppress(OSError):
                os.close(dir_fd)
