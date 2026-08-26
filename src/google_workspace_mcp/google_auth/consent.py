"""Run Google consent flow."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from .credentials import GoogleCredentials
from .errors import (
    GoogleAuthError,
    ScopeMismatchError,
    UnsafeCredentialPath,
)

OFFLINE_ACCESS_TYPE = 'offline'
CONSENT_PROMPT = 'consent'
LOOPBACK_HOST = '127.0.0.1'

MAX_CLIENT_SECRETS_BYTES = 64 * 1024

_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)

FlowFactory = Callable[[Mapping[str, Any], Sequence[str]], Any]


def _default_flow_factory(
    client_config: Mapping[str, Any],
    scopes: Sequence[str],
) -> Any:
    """Build installed application flow."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_config(
        dict(client_config), list(scopes)
    )


def _check_secret_metadata(metadata: os.stat_result) -> None:
    """Check owner and mode."""
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeCredentialPath('client secrets path is not a file')
    if metadata.st_uid != os.getuid():
        raise UnsafeCredentialPath('client secrets file has foreign owner')
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UnsafeCredentialPath('client secrets file is too permissive')


def _open_parent_dir_fd(target: Path) -> int:
    """Open secrets parent directory."""
    current_fd = os.open('/', _DIRECTORY_FLAGS)
    try:
        for part in target.parent.parts[1:]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError:
        os.close(current_fd)
        raise UnsafeCredentialPath(
            'client secrets path traversal failed'
        ) from None


def read_client_secrets(path: Path) -> Mapping[str, Any]:
    """Read validated client secrets."""
    target = Path(os.path.abspath(path))
    if target.parent == target:
        raise UnsafeCredentialPath('client secrets path is invalid')
    directory_fd = _open_parent_dir_fd(target)
    try:
        try:
            secret_fd = os.open(
                target.name,
                os.O_RDONLY | _NOFOLLOW,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            raise UnsafeCredentialPath(
                'client secrets file is missing'
            ) from None
        except OSError:
            raise UnsafeCredentialPath(
                'client secrets path is symlink'
            ) from None
        try:
            _check_secret_metadata(os.fstat(secret_fd))
            with os.fdopen(secret_fd, 'rb', closefd=False) as handle:
                raw = handle.read(MAX_CLIENT_SECRETS_BYTES + 1)
        finally:
            os.close(secret_fd)
    finally:
        os.close(directory_fd)
    if len(raw) > MAX_CLIENT_SECRETS_BYTES:
        raise UnsafeCredentialPath('client secrets file is too large')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise UnsafeCredentialPath(
            'client secrets file is not valid json'
        ) from None
    if not isinstance(payload, dict) or not (
        {'installed', 'web'} & set(payload)
    ):
        raise UnsafeCredentialPath('client secrets file has unknown shape')
    return payload


def validate_client_secrets_path(path: Path) -> None:
    """Validate client secrets file."""
    read_client_secrets(path)


def run_consent_flow(
    client_secrets_path: Path,
    scopes: Sequence[str],
    *,
    flow_factory: FlowFactory = _default_flow_factory,
    port: int = 0,
) -> GoogleCredentials:
    """Obtain durable Google credentials."""
    requested = tuple(scopes)
    if not requested:
        raise GoogleAuthError('consent requires at least one scope')
    client_config = read_client_secrets(client_secrets_path)

    flow = flow_factory(client_config, requested)
    with redirect_stdout(sys.stderr):
        library_credentials = flow.run_local_server(
            host=LOOPBACK_HOST,
            port=port,
            access_type=OFFLINE_ACCESS_TYPE,
            prompt=CONSENT_PROMPT,
            open_browser=False,
        )

    credentials = GoogleCredentials.from_google_credentials(
        library_credentials, fallback_scopes=requested
    )
    if not credentials.refresh_token:
        raise GoogleAuthError(
            'consent returned no refresh token, request offline access'
        )
    missing = set(requested).difference(credentials.scopes)
    if missing:
        raise ScopeMismatchError('consent granted fewer scopes than required')
    return credentials
