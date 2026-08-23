"""Audit logging transport module."""

import contextlib
import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    """Audit logging runtime error."""


def _ancestor_is_untrusted(st: os.stat_result) -> bool:
    """Detect untrusted ancestor entry."""
    if st.st_uid not in (0, os.getuid()):
        return True
    return bool(stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH))


def _validate_ancestry(path: Path) -> None:
    """Validate audit path ancestry."""
    curr = path.parent
    while True:
        if os.path.lexists(curr):
            st = os.lstat(curr)
            if not stat.S_ISLNK(st.st_mode) and not stat.S_ISDIR(st.st_mode):
                raise ValueError('invalid audit file target')
            if _ancestor_is_untrusted(st):
                raise ValueError('insecure audit path ancestor')
        if curr == curr.parent:
            return
        curr = curr.parent


def validate_audit_path(path: Path, download_path: Path) -> None:
    """Validate target audit path."""
    p = Path(os.path.abspath(path))
    dl = Path(os.path.abspath(download_path))
    if p == dl or p.is_relative_to(dl):
        raise ValueError('download_path collision')
    if os.path.lexists(p):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ValueError('invalid audit file target')
    parent = p.parent
    if os.path.lexists(parent):
        st = os.lstat(parent)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError('invalid audit file target')
        if stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError('insecure audit directory mode')
    _validate_ancestry(p)


class AuditLogger:
    """Secure audit logging handler."""

    def __init__(self, path: Path) -> None:
        """Initialize audit logger instance."""
        self._path = Path(os.path.abspath(path))

    def _ensure_dir(self, directory: Path) -> None:
        """Ensure secure directory ownership."""
        directory = directory.absolute()
        try:
            _validate_ancestry(directory / '_')
        except ValueError as exc:
            raise AuditError('Insecure directory target') from exc
        if os.path.lexists(directory):
            st = os.lstat(directory)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise AuditError('Insecure directory target')
            if st.st_uid != os.getuid():
                raise AuditError('Directory owner mismatch')
            if stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
                raise AuditError('Insecure directory mode')
            return

        missing: list[Path] = []
        curr = directory
        while not os.path.lexists(curr):
            missing.append(curr)
            curr = curr.parent

        st = os.lstat(curr)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise AuditError('Insecure directory target')
        if st.st_uid not in (0, os.getuid()):
            raise AuditError('Directory owner mismatch')

        for d in reversed(missing):
            with contextlib.suppress(FileExistsError):
                os.mkdir(d, 0o700)
            st = os.lstat(d)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise AuditError('Insecure directory target')
            if st.st_uid != os.getuid():
                raise AuditError('Directory owner mismatch')
            os.chmod(d, 0o700, follow_symlinks=False)

    def log_event(self, event: dict[str, Any]) -> None:
        """Record secure audit event."""
        try:
            self._ensure_dir(self._path.parent)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, 'O_NOFOLLOW'):
                flags |= os.O_NOFOLLOW
            if hasattr(os, 'O_NONBLOCK'):
                flags |= os.O_NONBLOCK
            fd = os.open(self._path, flags, 0o600)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                    raise AuditError('Insecure file target')
                os.fchmod(fd, 0o600)
                if hasattr(os, 'O_NONBLOCK'):
                    flags_curr = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags_curr & ~os.O_NONBLOCK)
                fcntl.flock(fd, fcntl.LOCK_EX)
                data = json.dumps(event, separators=(',', ':')) + '\n'
                payload = memoryview(data.encode())
                while payload:
                    written = os.write(fd, payload)
                    if written <= 0:
                        raise AuditError('Incomplete audit record write')
                    payload = payload[written:]
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except AuditError:
            raise
        except Exception:
            raise AuditError('Failed to record audit event') from None
