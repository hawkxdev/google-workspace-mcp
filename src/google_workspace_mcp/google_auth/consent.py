"""Run Google consent flow."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Sequence
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

FlowFactory = Callable[[str, Sequence[str]], Any]


def _default_flow_factory(secrets_path: str, scopes: Sequence[str]) -> Any:
    """Build installed application flow."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_secrets_file(
        secrets_path, list(scopes)
    )


def validate_client_secrets_path(path: Path) -> None:
    """Validate client secrets file."""
    target = Path(os.path.abspath(path))
    if not os.path.lexists(target):
        raise UnsafeCredentialPath('client secrets file is missing')
    metadata = os.lstat(target)
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafeCredentialPath('client secrets path is symlink')
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeCredentialPath('client secrets path is not a file')
    if metadata.st_uid != os.getuid():
        raise UnsafeCredentialPath('client secrets file has foreign owner')
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UnsafeCredentialPath('client secrets file is too permissive')


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
    validate_client_secrets_path(client_secrets_path)

    flow = flow_factory(
        str(Path(os.path.abspath(client_secrets_path))), requested
    )
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
