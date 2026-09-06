"""Warm up Google service credentials.

Google retires an OAuth client and a refresh token after six months of
inactivity, and it measures that inactivity by token exchanges rather
than by use of an already issued access token. A rarely called service
therefore loses access on a date chosen by the calendar. This entrypoint
forces one exchange per service and follows it with one minimal
read-only call, so both counters restart.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.cli.authorize import SERVICE_SCOPES, _os_detail
from google_workspace_mcp.common.config import (
    resolve_download_path,
    resolve_token_path,
)
from google_workspace_mcp.google_auth import (
    GoogleAuthError,
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.google_auth.errors import (
    ScopeMismatchError,
    TokenRevokedError,
    UnsafeCredentialPath,
)
from google_workspace_mcp.services.calendar.client import (
    build_calendar_service,
)
from google_workspace_mcp.services.drive.client import build_drive_service
from google_workspace_mcp.services.gmail.client import build_gmail_service

StoreFactory = Callable[[str], Any]
ProbeRunner = Callable[[str, GoogleCredentials], None]

# The warm-up owns its retry budget instead of inheriting a service one.
WARMUP_REQUEST_RETRIES = 2

_UNEXPECTED_FAILURE = 'warm-up failed before the credential was refreshed'

# Sheets and Docs address a single document and expose no cheap listing,
# so both reuse the Drive listing their `drive.file` scope already
# covers. An empty result is a valid success: `drive.file` only shows
# what this application created.
PROBE_NAMES: dict[str, str] = {
    'gmail': 'gmail.users.getProfile',
    'calendar': 'calendar.calendarList.list',
    'drive': 'drive.files.list',
    'sheets': 'drive.files.list',
    'docs': 'drive.files.list',
}


def _default_store(service: str) -> GoogleCredentialStore:
    """Build the store for one service."""
    return GoogleCredentialStore(
        resolve_token_path(service),
        resolve_download_path(service),
        SERVICE_SCOPES[service],
    )


def _default_probe(service: str, credentials: GoogleCredentials) -> None:
    """Perform one minimal read-only Google call."""
    if service == 'gmail':
        client = build_gmail_service(credentials)
        client.users().getProfile(userId='me').execute(
            num_retries=WARMUP_REQUEST_RETRIES
        )
        return
    if service == 'calendar':
        client = build_calendar_service(credentials)
        client.calendarList().list(maxResults=1, fields='items(id)').execute(
            num_retries=WARMUP_REQUEST_RETRIES
        )
        return
    client = build_drive_service(credentials)
    client.files().list(pageSize=1, fields='files(id)').execute(
        num_retries=WARMUP_REQUEST_RETRIES
    )


def _safe_message(exc: BaseException) -> str:
    """Build secret free message for the refresh stage.

    The failure is classified by exception type and the wording comes
    from here. Echoing str(exc) would publish whatever text the raising
    layer happened to embed, which is exactly the value this command
    must never print.
    """
    try:
        if isinstance(exc, TokenRevokedError):
            return 'Google authorization requires renewal'
        if isinstance(exc, ScopeMismatchError):
            return 'credentials are missing required scopes'
        if isinstance(exc, UnsafeCredentialPath):
            return 'credential path is unsafe'
        if isinstance(exc, GoogleAuthError):
            return 'Google credential refresh is unavailable'
        if isinstance(exc, OSError):
            return f'credential path is unusable: {_os_detail(exc.errno)}'
    except Exception:
        return _UNEXPECTED_FAILURE
    return _UNEXPECTED_FAILURE


def _probe_message(exc: BaseException) -> str:
    """Build secret free message for the probe stage.

    The probe runs after a successful token exchange, so its failures
    must never borrow the refresh-stage wording. Saying the credential
    was not refreshed here would push an operator toward re-running the
    interactive consent flow, spending one of the hundred refresh tokens
    this command exists to preserve.

    The probe reaches Google over the network, so an OSError here is a
    transport failure, not a filesystem one: socket.gaierror, TimeoutError
    and ssl.SSLError are all OSError subclasses, and gaierror carries an
    EAI code that os.strerror cannot render.
    """
    try:
        status = getattr(getattr(exc, 'resp', None), 'status', None)
        if isinstance(status, int):
            return (
                f'credential refreshed, but the probe returned HTTP {status}'
            )
        if isinstance(exc, OSError):
            return 'credential refreshed, but the probe could not reach Google'
    except Exception:
        return 'credential refreshed, but the probe failed'
    return 'credential refreshed, but the probe failed'


def _parser() -> argparse.ArgumentParser:
    """Build warm-up argument parser."""
    parser = argparse.ArgumentParser(
        prog='google-mcp-warmup',
        description=(
            'Force one Google token exchange per service and perform one '
            'minimal read-only call, so provider inactivity timers do not '
            'retire the OAuth client or the refresh token.'
        ),
    )
    parser.add_argument('--service', choices=SERVICES)
    return parser


def _emit(stream: TextIO, payload: object) -> None:
    """Write one json line."""
    json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
    stream.write('\n')


def main(
    argv: list[str] | None = None,
    out: TextIO | None = None,
    errors: TextIO | None = None,
    store_factory: StoreFactory = _default_store,
    probe_runner: ProbeRunner = _default_probe,
) -> int:
    """Run warm-up entrypoint."""
    args = _parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    error_stream = errors if errors is not None else sys.stderr

    services = (args.service,) if args.service else SERVICES
    failures = 0
    for service in services:
        # The two stages are reported separately on purpose: after the
        # exchange succeeds the inactivity timers are already reset, and
        # a probe failure must not read as "nothing happened".
        try:
            store = store_factory(service)
            # Force the exchange: a live access token would otherwise
            # short-circuit the refresh and touch no provider counter.
            credentials = store.refresh(force=True)
        except KeyboardInterrupt, SystemExit:
            raise
        except Exception as exc:
            _emit(
                error_stream,
                {
                    'service': service,
                    'refreshed': False,
                    'error': _safe_message(exc),
                },
            )
            failures += 1
            continue

        try:
            probe_runner(service, credentials)
        except KeyboardInterrupt, SystemExit:
            raise
        except Exception as exc:
            _emit(
                error_stream,
                {
                    'service': service,
                    'refreshed': True,
                    'error': _probe_message(exc),
                },
            )
            failures += 1
            continue

        _emit(
            stream,
            {
                'service': service,
                'token_path': str(store.path),
                'refreshed': True,
                'probe': PROBE_NAMES[service],
                'expiry': (
                    credentials.expiry.isoformat()
                    if credentials.expiry is not None
                    else None
                ),
            },
        )

    return 1 if failures else 0


def _entrypoint() -> None:
    """Run warm-up entrypoint wrapper."""
    raise SystemExit(main())


if __name__ == '__main__':
    _entrypoint()
