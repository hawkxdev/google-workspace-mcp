"""Store secure Google credentials."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import stat
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request

from google_workspace_mcp.common.retry import execute_with_retry
from google_workspace_mcp.google_auth.credentials import GoogleCredentials
from google_workspace_mcp.google_auth.errors import (
    GoogleAuthError,
    ScopeMismatchError,
    TokenRevokedError,
    UnsafeCredentialPath,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def validate_credential_path(
    path: Path,
    download_path: Path | None = None,
) -> None:
    """Validate credential path boundaries."""
    target = Path(os.path.abspath(path))
    if download_path is not None:
        download = Path(os.path.abspath(download_path))
        if target == download or target.is_relative_to(download):
            raise UnsafeCredentialPath('credential path collision')

    current = target.parent
    while True:
        if os.path.lexists(current):
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeCredentialPath('credential ancestor is symlink')
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafeCredentialPath('credential ancestor is invalid')
        if current == current.parent:
            break
        current = current.parent

    if os.path.lexists(target):
        metadata = os.lstat(target)
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeCredentialPath('credential target is symlink')
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeCredentialPath('credential target is invalid')


def _refresh_error_data(exc: RefreshError) -> Mapping[str, Any] | None:
    """Extract structured refresh error."""
    if len(exc.args) < 2:
        return None
    data = exc.args[1]
    if isinstance(data, Mapping):
        return data
    return None


class GoogleCredentialStore:
    """Manage secure Google credentials."""

    def __init__(
        self,
        path: Path,
        download_path: Path | None = None,
        required_scopes: tuple[str, ...] = (),
    ) -> None:
        """Initialize Google credential store."""
        self._path = Path(os.path.abspath(path))
        self._download_path = (
            Path(os.path.abspath(download_path))
            if download_path is not None
            else None
        )
        self._required_scopes = tuple(required_scopes)

    @property
    def path(self) -> Path:
        """Return credential file path."""
        return self._path

    @property
    def required_scopes(self) -> tuple[str, ...]:
        """Return required credential scopes."""
        return self._required_scopes

    def _open_target_dir_fd(self) -> int:
        """Open secure target directory."""
        parts = self._path.parent.parts
        current_fd = os.open('/', _DIRECTORY_FLAGS)
        try:
            for part in parts[1:]:
                created = False
                try:
                    next_fd = os.open(
                        part,
                        _DIRECTORY_FLAGS,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                        created = True
                    except FileExistsError:
                        pass
                    next_fd = os.open(
                        part,
                        _DIRECTORY_FLAGS,
                        dir_fd=current_fd,
                    )
                if created:
                    try:
                        os.fchmod(next_fd, 0o700)
                    except Exception:
                        os.close(next_fd)
                        raise
                previous_fd = current_fd
                current_fd = next_fd
                os.close(previous_fd)

            metadata = os.fstat(current_fd)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != os.getuid() or mode != 0o700:
                raise UnsafeCredentialPath('unsafe credential directory')
            return current_fd
        except UnsafeCredentialPath:
            os.close(current_fd)
            raise
        except Exception:
            os.close(current_fd)
            raise UnsafeCredentialPath('credential traversal failed') from None

    def _open_lock_fd(self, directory_fd: int) -> int:
        """Open secure lock file."""
        lock_name = f'{self._path.name}.lock'
        created = False
        try:
            try:
                lock_fd = os.open(
                    lock_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                created = True
            except FileExistsError:
                lock_fd = os.open(
                    lock_name,
                    os.O_RDWR | _NOFOLLOW,
                    dir_fd=directory_fd,
                )
            metadata = os.fstat(lock_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeCredentialPath('unsafe lock file target')
            if metadata.st_uid != os.getuid():
                raise UnsafeCredentialPath('unsafe lock file owner')
            if created:
                os.fchmod(lock_fd, 0o600)
                metadata = os.fstat(lock_fd)
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise UnsafeCredentialPath('unsafe lock permissions')
            return lock_fd
        except UnsafeCredentialPath:
            if 'lock_fd' in locals():
                os.close(lock_fd)
            raise
        except Exception:
            if 'lock_fd' in locals():
                os.close(lock_fd)
            raise UnsafeCredentialPath('failed to open lock file') from None

    @contextmanager
    def _file_lock(self, directory_fd: int) -> Generator[None]:
        """Hold exclusive credential lock."""
        lock_fd = self._open_lock_fd(directory_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _load_locked(self, directory_fd: int) -> GoogleCredentials | None:
        """Load credentials under lock."""
        try:
            credential_fd = os.open(
                self._path.name,
                os.O_RDONLY | _NOFOLLOW,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except Exception:
            raise UnsafeCredentialPath(
                'failed to open credential file'
            ) from None

        try:
            metadata = os.fstat(credential_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeCredentialPath('unsafe credential file target')
            if metadata.st_uid != os.getuid():
                raise UnsafeCredentialPath('unsafe credential file owner')
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise UnsafeCredentialPath('insecure file mode')
            try:
                with os.fdopen(
                    credential_fd,
                    'r',
                    encoding='utf-8',
                    closefd=False,
                ) as stream:
                    data = json.load(stream)
            except Exception:
                raise GoogleAuthError('failed to load credentials') from None
            if not isinstance(data, dict):
                raise GoogleAuthError('failed to load credentials')
            try:
                return GoogleCredentials.from_dict(data)
            except TypeError, ValueError:
                raise GoogleAuthError('failed to load credentials') from None
        finally:
            os.close(credential_fd)

    def preflight(self) -> None:
        """Prove target path usable."""
        validate_credential_path(self._path, self._download_path)
        directory_fd = self._open_target_dir_fd()
        try:
            with self._file_lock(directory_fd):
                pass
        finally:
            os.close(directory_fd)

    def load(self) -> GoogleCredentials | None:
        """Load stored Google credentials."""
        validate_credential_path(self._path, self._download_path)
        directory_fd = self._open_target_dir_fd()
        try:
            with self._file_lock(directory_fd):
                return self._load_locked(directory_fd)
        finally:
            os.close(directory_fd)

    def _save_locked(
        self,
        credentials: GoogleCredentials,
        directory_fd: int,
    ) -> None:
        """Save credentials under lock."""
        payload = json.dumps(
            credentials.to_dict(),
            separators=(',', ':'),
        ).encode()
        temporary_name = f'.tmp_{os.getpid()}_{secrets.token_hex(12)}'
        temporary_fd: int | None = None
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(temporary_fd, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError('incomplete credential write')
                remaining = remaining[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(
                temporary_name,
                self._path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except Exception:
            if temporary_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(temporary_fd)
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory_fd)
            raise GoogleAuthError('failed to save credentials') from None

    def save(self, credentials: GoogleCredentials) -> None:
        """Save stored Google credentials."""
        validate_credential_path(self._path, self._download_path)
        directory_fd = self._open_target_dir_fd()
        try:
            with self._file_lock(directory_fd):
                self._save_locked(credentials, directory_fd)
        finally:
            os.close(directory_fd)

    def _validate_scopes(self, scopes: tuple[str, ...]) -> None:
        """Validate effective credential scopes."""
        missing = set(self._required_scopes).difference(scopes)
        if missing:
            raise ScopeMismatchError('credentials missing required scopes')

    def get_credentials(self) -> GoogleCredentials:
        """Return validated Google credentials."""
        credentials = self.load()
        if credentials is None:
            raise GoogleAuthError('credentials are not configured')
        self._validate_scopes(credentials.scopes)
        return credentials

    def refresh(self, request: Any = None) -> GoogleCredentials:
        """Refresh authorized Google credentials."""
        validate_credential_path(self._path, self._download_path)
        directory_fd = self._open_target_dir_fd()
        try:
            with self._file_lock(directory_fd):
                stored = self._load_locked(directory_fd)
                if stored is None:
                    raise GoogleAuthError('credentials are not configured')
                library_credentials = stored.to_google_credentials()
                if stored.expiry is not None and library_credentials.valid:
                    self._validate_scopes(stored.scopes)
                    return stored
                resolved_request = request or Request()
                try:
                    execute_with_retry(
                        lambda: library_credentials.refresh(resolved_request)
                    )
                except RefreshError as exc:
                    data = _refresh_error_data(exc)
                    if (
                        data is not None
                        and data.get('error') == 'invalid_grant'
                    ):
                        raise TokenRevokedError(
                            'Google authorization requires renewal'
                        ) from None
                    if exc.retryable:
                        raise GoogleAuthError(
                            'Google credential refresh is unavailable'
                        ) from None
                    raise GoogleAuthError(
                        'Google authorization requires renewal'
                    ) from None
                except TransportError, TimeoutError, ConnectionError:
                    raise GoogleAuthError(
                        'Google credential refresh is unavailable'
                    ) from None

                effective_scopes = (
                    tuple(library_credentials.granted_scopes)
                    if library_credentials.granted_scopes is not None
                    else stored.scopes
                )
                self._validate_scopes(effective_scopes)
                updated = GoogleCredentials.from_google_credentials(
                    library_credentials,
                    fallback_scopes=effective_scopes,
                )
                self._save_locked(updated, directory_fd)
                return updated
        finally:
            os.close(directory_fd)
