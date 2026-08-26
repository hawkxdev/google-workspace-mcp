"""Obtain Google service credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.common.config import (
    resolve_download_path,
    resolve_token_path,
)
from google_workspace_mcp.google_auth import (
    GoogleAuthError,
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.google_auth.consent import (
    run_consent_flow,
    validate_client_secrets_path,
)
from google_workspace_mcp.services.calendar.constants import CALENDAR_SCOPES
from google_workspace_mcp.services.docs.constants import DOCS_SCOPES
from google_workspace_mcp.services.drive.constants import DRIVE_SCOPES
from google_workspace_mcp.services.gmail.constants import GMAIL_SCOPE
from google_workspace_mcp.services.sheets.constants import SHEETS_SCOPES

SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    'gmail': (GMAIL_SCOPE,),
    'calendar': CALENDAR_SCOPES,
    'drive': DRIVE_SCOPES,
    'sheets': SHEETS_SCOPES,
    'docs': DOCS_SCOPES,
}

ConsentRunner = Callable[[Path, tuple[str, ...], int], GoogleCredentials]

_UNEXPECTED_FAILURE = 'authorization failed before credentials were stored'


def _default_consent(
    client_secrets: Path,
    scopes: tuple[str, ...],
    port: int,
) -> GoogleCredentials:
    """Run interactive Google consent."""
    return run_consent_flow(client_secrets, scopes, port=port)


def _os_detail(errno_value: object) -> str:
    """Describe errno without secrets."""
    # OSError built with two non integer arguments carries a str errno
    if isinstance(errno_value, bool) or not isinstance(errno_value, int):
        return 'os error'
    return os.strerror(errno_value)


def _safe_message(exc: BaseException) -> str:
    """Build secret free message."""
    try:
        if isinstance(exc, GoogleAuthError):
            return str(exc)
        if isinstance(exc, OSError):
            return f'credential path is unusable: {_os_detail(exc.errno)}'
    except Exception:
        return _UNEXPECTED_FAILURE
    return _UNEXPECTED_FAILURE


def _port(value: str) -> int:
    """Parse bounded loopback port."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer') from None
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError('port must be between 0 and 65535')
    return parsed


def _parser() -> argparse.ArgumentParser:
    """Build authorization argument parser."""
    parser = argparse.ArgumentParser(
        prog='google-mcp-authorize',
        description=(
            'Obtain a durable Google refresh token for one service and '
            'store it under the service credential path.'
        ),
    )
    parser.add_argument('--service', choices=SERVICES, required=True)
    parser.add_argument('--client-secrets', type=Path, required=True)
    parser.add_argument('--token-path', type=Path)
    parser.add_argument('--download-path', type=Path)
    parser.add_argument('--port', type=_port, default=0)
    return parser


def _emit(stream: TextIO, payload: object) -> None:
    """Write one json line."""
    json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
    stream.write('\n')


def main(
    argv: list[str] | None = None,
    out: TextIO | None = None,
    errors: TextIO | None = None,
    consent_runner: ConsentRunner = _default_consent,
) -> int:
    """Run authorization entrypoint."""
    args = _parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    error_stream = errors if errors is not None else sys.stderr

    scopes = SERVICE_SCOPES[args.service]
    try:
        token_path = args.token_path or resolve_token_path(args.service)
        download_path = args.download_path or resolve_download_path(
            args.service
        )
        store = GoogleCredentialStore(token_path, download_path, scopes)
        # 1. Reject a bad secrets file before creating any state
        validate_client_secrets_path(args.client_secrets)
        # 2. Prove the target path before any live grant exists
        store.preflight()
        # 3. Obtain the grant
        credentials = consent_runner(args.client_secrets, scopes, args.port)
        # 4. Persist it
        store.save(credentials)
    except KeyboardInterrupt, SystemExit:
        raise
    except Exception as exc:
        _emit(error_stream, {'error': _safe_message(exc)})
        return 1

    _emit(
        stream,
        {
            'service': args.service,
            'token_path': str(token_path),
            'granted_scopes': list(credentials.scopes),
            'refresh_token_present': bool(credentials.refresh_token),
            'expiry': (
                credentials.expiry.isoformat()
                if credentials.expiry is not None
                else None
            ),
        },
    )
    return 0


def _entrypoint() -> None:
    """Run authorization entrypoint wrapper."""
    raise SystemExit(main())


if __name__ == '__main__':
    _entrypoint()
