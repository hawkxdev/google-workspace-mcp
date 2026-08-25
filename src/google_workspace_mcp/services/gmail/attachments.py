"""Store managed Gmail attachments."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from pathlib import Path

from .constants import MAX_ATTACHMENT_BYTES
from .errors import GmailAttachmentError, GmailPayloadError
from .mime import decode_base64url
from .schemas import DownloadedAttachment

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')


def _safe_component(value: str, fallback: str) -> str:
    """Sanitize managed filename component."""
    sanitized = _SAFE_NAME.sub('_', value).strip('._')
    return sanitized or fallback


class ManagedAttachmentStore:
    """Publish managed Gmail attachments."""

    def __init__(
        self,
        directory: Path,
        max_bytes: int = MAX_ATTACHMENT_BYTES,
    ) -> None:
        """Initialize managed attachment store."""
        self._directory = Path(os.path.abspath(directory))
        self._max_bytes = max_bytes

    def _open_directory(self) -> int:
        """Open safe download directory."""
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
            raise GmailAttachmentError(
                'download directory is unsafe'
            ) from None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise GmailAttachmentError('download directory is unsafe')
        return descriptor

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        """Write complete attachment bytes."""
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise GmailAttachmentError('attachment write failed')
            remaining = remaining[written:]

    def save(
        self,
        message_id: str,
        attachment_id: str,
        filename: str,
        expected_size: int,
        encoded_data: str,
    ) -> DownloadedAttachment:
        """Save one managed attachment."""
        if expected_size < 0 or expected_size > self._max_bytes:
            raise GmailAttachmentError('attachment is too large')
        global_encoded_limit = 4 * ((self._max_bytes + 2) // 3)
        if len(encoded_data) > global_encoded_limit:
            raise GmailAttachmentError('attachment is too large')
        padding = 0
        if encoded_data.endswith('=='):
            padding = 2
        elif encoded_data.endswith('='):
            padding = 1
        payload_length = len(encoded_data) - padding
        if (
            encoded_data.find('=') not in {-1, payload_length}
            or payload_length % 4 == 1
            or padding not in {0, (-payload_length) % 4}
        ):
            raise GmailAttachmentError('attachment encoding is invalid')
        expected_encoded_limit = 4 * ((expected_size + 2) // 3)
        if len(encoded_data) > expected_encoded_limit:
            raise GmailAttachmentError('attachment size is invalid')
        try:
            encoded_data.encode('ascii')
        except UnicodeEncodeError:
            raise GmailAttachmentError(
                'attachment encoding is invalid'
            ) from None
        try:
            data = decode_base64url(encoded_data)
        except GmailPayloadError:
            raise GmailAttachmentError(
                'attachment encoding is invalid'
            ) from None
        if len(data) > self._max_bytes:
            raise GmailAttachmentError('attachment is too large')
        if len(data) != expected_size:
            raise GmailAttachmentError('attachment size is invalid')
        safe_message = _safe_component(message_id, 'message')[:40]
        safe_attachment = _safe_component(attachment_id, 'attachment')[:40]
        provider_name = Path(filename).name
        safe_filename = _safe_component(provider_name, 'attachment.bin')
        suffix = Path(safe_filename).suffix[:20]
        stem_limit = 140 - len(suffix)
        safe_filename = f'{Path(safe_filename).stem[:stem_limit]}{suffix}'
        final_name = f'{safe_message}_{safe_attachment}_{safe_filename}'
        temp_name = f'.tmp_{secrets.token_hex(12)}'
        directory_fd = self._open_directory()
        temp_fd: int | None = None
        linked = False
        published = False
        try:
            try:
                os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise GmailAttachmentError('attachment already exists')
            temp_fd = os.open(
                temp_name,
                _CREATE_FLAGS,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(temp_fd, 0o600)
            self._write_all(temp_fd, data)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            try:
                os.link(
                    temp_name,
                    final_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise GmailAttachmentError(
                    'attachment already exists'
                ) from None
            linked = True
            os.unlink(temp_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            published = True
        except GmailAttachmentError:
            raise
        except OSError:
            raise GmailAttachmentError('attachment write failed') from None
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=directory_fd)
            if linked and not published:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(final_name, dir_fd=directory_fd)
                with contextlib.suppress(OSError):
                    os.fsync(directory_fd)
            os.close(directory_fd)
        return DownloadedAttachment(
            path=str(self._directory / final_name),
            filename=final_name,
            size=len(data),
        )
