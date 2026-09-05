"""Persist evaluation OAuth state."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# === Constants ===

STORAGE_VERSION = 2


class FileTokenStorage:
    """Store one service OAuth state."""

    __slots__ = ('_path',)

    def __init__(self, path: Path) -> None:
        """Configure one storage file."""
        self._path = path

    def __repr__(self) -> str:
        """Return a secret safe representation."""
        return 'FileTokenStorage()'

    async def get_tokens(self) -> OAuthToken | None:
        """Get stored OAuth tokens."""
        payload = await asyncio.to_thread(self._read_state)
        tokens = payload.get('tokens')
        if tokens is None:
            return None
        try:
            return OAuthToken.model_validate(tokens)
        except Exception as error:
            raise ValueError('OAuth state is invalid') from error

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Store OAuth tokens."""
        expires_at = (
            time.time() + tokens.expires_in
            if tokens.expires_in is not None
            else None
        )
        await asyncio.to_thread(
            self._update_state,
            lambda payload: payload.update(
                tokens=tokens.model_dump(mode='json'),
                token_expires_at=expires_at,
            ),
        )

    async def get_token_expiry(self) -> float | None:
        """Get the stored absolute token expiry."""
        payload = await asyncio.to_thread(self._read_state)
        expires_at = payload.get('token_expires_at')
        if expires_at is None:
            return None
        if isinstance(expires_at, bool) or not isinstance(
            expires_at,
            int | float,
        ):
            raise ValueError('OAuth state is invalid')
        return float(expires_at)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Get stored client information."""
        payload = await asyncio.to_thread(self._read_state)
        client_info = payload.get('client_info')
        if client_info is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(client_info)
        except Exception as error:
            raise ValueError('OAuth state is invalid') from error

    async def set_client_info(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Store client information."""
        await asyncio.to_thread(
            self._update_state,
            lambda payload: payload.update(
                client_info=client_info.model_dump(mode='json')
            ),
        )

    def _directory_fd(self, *, create: bool) -> int | None:
        """Open one protected directory."""
        directory = self._path.parent
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                directory.mkdir(mode=0o700)
                metadata = directory.lstat()
            except OSError as error:
                raise ValueError('OAuth directory is unavailable') from error
        except OSError as error:
            raise ValueError('OAuth directory is unavailable') from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError('OAuth directory must be a directory')
        if metadata.st_uid != os.getuid():
            raise ValueError('OAuth directory has a foreign owner')
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('OAuth directory mode must be 0700')
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            return os.open(directory, flags)
        except OSError as error:
            raise ValueError('OAuth directory is unavailable') from error

    def _read_payload(self, directory_fd: int) -> dict[str, Any]:
        """Read one protected state file."""
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return self._empty_state()
        except OSError as error:
            raise ValueError('OAuth file is unavailable') from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('OAuth file must be regular')
        if metadata.st_uid != os.getuid():
            raise ValueError('OAuth file has a foreign owner')
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError('OAuth file mode must be 0600')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(self._path.name, flags, dir_fd=directory_fd)
        except OSError as error:
            raise ValueError('OAuth file is unavailable') from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError('OAuth file must be regular')
            if (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise ValueError('OAuth file changed during open')
            with os.fdopen(descriptor, encoding='utf-8') as state_file:
                descriptor = -1
                payload = json.load(state_file)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError('OAuth state is invalid') from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return self._validate_payload(payload)

    def _read_state(self) -> dict[str, Any]:
        """Read state under a file lock."""
        directory_fd = self._directory_fd(create=False)
        if directory_fd is None:
            return self._empty_state()
        try:
            return self._read_payload(directory_fd)
        finally:
            os.close(directory_fd)

    def _update_state(
        self,
        update: Callable[[dict[str, Any]], None],
    ) -> None:
        """Update state under a file lock."""
        directory_fd = self._directory_fd(create=True)
        if directory_fd is None:
            raise ValueError('OAuth directory is unavailable')
        lock_name = f'.{self._path.name}.lock'
        lock_fd = self._open_lock(directory_fd, lock_name)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            payload = self._read_payload(directory_fd)
            update(payload)
            self._write_payload(directory_fd, payload)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            os.close(directory_fd)

    def _open_lock(self, directory_fd: int, name: str) -> int:
        """Open one protected lock file."""
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise ValueError('OAuth lock file is invalid')
            return descriptor
        except Exception:
            with contextlib.suppress(UnboundLocalError, OSError):
                os.close(descriptor)
            raise

    def _write_payload(
        self,
        directory_fd: int,
        payload: dict[str, Any],
    ) -> None:
        """Atomically write one state file."""
        encoded = (
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
            + '\n'
        ).encode('utf-8')
        temporary_name = (
            f'.{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp'
        )
        temporary_fd: int | None = None
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, 'O_NOFOLLOW', 0),
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(temporary_fd, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError('incomplete OAuth state write')
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
        finally:
            if temporary_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(temporary_fd)
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory_fd)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        """Build one empty state."""
        return {
            'version': STORAGE_VERSION,
            'tokens': None,
            'token_expires_at': None,
            'client_info': None,
        }

    @staticmethod
    def _validate_payload(payload: object) -> dict[str, Any]:
        """Validate one state document."""
        if not isinstance(payload, dict):
            raise ValueError('OAuth state is invalid')
        if set(payload) != {
            'version',
            'tokens',
            'token_expires_at',
            'client_info',
        }:
            raise ValueError('OAuth state is invalid')
        if payload.get('version') != STORAGE_VERSION:
            raise ValueError('OAuth state is invalid')
        tokens = payload.get('tokens')
        token_expires_at = payload.get('token_expires_at')
        client_info = payload.get('client_info')
        if tokens is not None and not isinstance(tokens, dict):
            raise ValueError('OAuth state is invalid')
        if token_expires_at is not None and (
            isinstance(token_expires_at, bool)
            or not isinstance(token_expires_at, int | float)
        ):
            raise ValueError('OAuth state is invalid')
        if client_info is not None and not isinstance(client_info, dict):
            raise ValueError('OAuth state is invalid')
        return dict(payload)
