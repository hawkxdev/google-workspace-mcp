"""Test Google credential layer."""

from __future__ import annotations

import http.server
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials as OAuth2Credentials

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.common.retry import (
    RetryConfig,
    execute_with_retry,
    is_retryable_google_error,
)
from google_workspace_mcp.google_auth.credentials import GoogleCredentials
from google_workspace_mcp.google_auth.errors import (
    GoogleAuthError,
    ScopeMismatchError,
    TokenRevokedError,
    UnsafeCredentialPath,
)
from google_workspace_mcp.google_auth.store import (
    GoogleCredentialStore,
    validate_credential_path,
)


class FakeTokenServer:
    """Serve fake token responses."""

    def __init__(
        self,
        *,
        scope: str | None = None,
        delay: float = 0.0,
    ) -> None:
        """Initialize token test server."""
        self.scope = scope
        self.delay = delay
        self.request_count = 0
        self._count_lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """Handle fake token requests."""

            def do_POST(self) -> None:
                """Return fake token response."""
                with outer._count_lock:
                    outer.request_count += 1
                length = int(self.headers.get('Content-Length', '0'))
                if length:
                    self.rfile.read(length)
                if outer.delay:
                    time.sleep(outer.delay)
                response: dict[str, Any] = {
                    'access_token': 'refreshed-token-payload',
                    'expires_in': 3600,
                    'token_type': 'Bearer',
                }
                if outer.scope is not None:
                    response['scope'] = outer.scope
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                """Suppress fake server logs."""

        self._server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

    @property
    def token_uri(self) -> str:
        """Return fake token endpoint."""
        host, port = self._server.server_address
        return f'http://{host}:{port}/token'

    def close(self) -> None:
        """Stop fake token server."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _credentials(
    *,
    token: str = 'access-token',
    refresh_token: str = 'refresh-token',
    token_uri: str = 'https://oauth2.googleapis.com/token',
    scopes: tuple[str, ...] = (),
) -> GoogleCredentials:
    """Build synthetic Google credentials."""
    return GoogleCredentials(
        token=token,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id='client-id',
        client_secret='client-secret',
        scopes=scopes,
    )


_WORKER_SCRIPT = """
from pathlib import Path
import sys
import time

from google_workspace_mcp.google_auth.store import GoogleCredentialStore

path = Path(sys.argv[1])
ready = Path(sys.argv[2])
gate = Path(sys.argv[3])
ready.write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 5
while not gate.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('worker barrier timeout')
    time.sleep(0.01)
credentials = GoogleCredentialStore(path).refresh()
print(credentials.token or '')
"""


def test_credentials_repr_hides_secrets() -> None:
    credentials = _credentials(
        token='secret-access',
        refresh_token='secret-refresh',
    )
    rendered = repr(credentials)
    assert 'secret-access' not in rendered
    assert 'secret-refresh' not in rendered
    assert 'client-secret' not in rendered


def test_validate_credential_path_rejects_boundaries(
    tmp_path: Path,
) -> None:
    download = tmp_path / 'downloads'
    download.mkdir(mode=0o700)
    validate_credential_path(tmp_path / 'tokens' / 'token.json', download)

    with pytest.raises(UnsafeCredentialPath):
        validate_credential_path(download / 'token.json', download)

    target = tmp_path / 'target'
    target.mkdir(mode=0o700)
    symlink = tmp_path / 'symlink'
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(UnsafeCredentialPath):
        validate_credential_path(symlink / 'token.json', download)


def test_store_rejects_wrong_modes(tmp_path: Path) -> None:
    for index, mode in enumerate((0o755, 0o500)):
        directory = tmp_path / f'directory-{index}'
        directory.mkdir(mode=0o700)
        os.chmod(directory, mode)
        store = GoogleCredentialStore(directory / 'token.json')
        with pytest.raises(UnsafeCredentialPath):
            store.load()

    secure = tmp_path / 'secure'
    secure.mkdir(mode=0o700)
    for index, mode in enumerate((0o644, 0o400)):
        token_path = secure / f'token-{index}.json'
        token_path.write_text('{}', encoding='utf-8')
        os.chmod(token_path, mode)
        store = GoogleCredentialStore(token_path)
        with pytest.raises(UnsafeCredentialPath):
            store.load()

    lock_target = secure / 'locked.json'
    lock_path = secure / 'locked.json.lock'
    lock_path.write_text('', encoding='utf-8')
    os.chmod(lock_path, 0o644)
    with pytest.raises(UnsafeCredentialPath):
        GoogleCredentialStore(lock_target).save(_credentials())


def test_held_directory_descriptor_resists_swap(tmp_path: Path) -> None:
    trusted = tmp_path / 'trusted'
    trusted.mkdir(mode=0o700)
    attacker = tmp_path / 'attacker'
    attacker.mkdir(mode=0o700)
    store = GoogleCredentialStore(trusted / 'token.json')

    directory_fd = store._open_target_dir_fd()
    try:
        renamed = tmp_path / 'renamed'
        trusted.rename(renamed)
        trusted.symlink_to(attacker, target_is_directory=True)
        store._save_locked(_credentials(), directory_fd)
    finally:
        os.close(directory_fd)

    assert (renamed / 'token.json').exists()
    assert not (attacker / 'token.json').exists()


def test_directory_setup_closes_descriptor_on_failure(
    tmp_path: Path,
) -> None:
    store = GoogleCredentialStore(tmp_path / 'new-dir' / 'token.json')
    descriptors_before = set(os.listdir('/dev/fd'))
    with (
        patch('os.fchmod', side_effect=OSError('mode failure')),
        pytest.raises(UnsafeCredentialPath),
    ):
        store.load()
    descriptors_after = set(os.listdir('/dev/fd'))
    assert descriptors_after == descriptors_before


def test_store_round_trip_and_exact_modes(tmp_path: Path) -> None:
    directory = tmp_path / 'secure'
    directory.mkdir(mode=0o700)
    token_path = directory / 'token.json'
    store = GoogleCredentialStore(token_path)

    old_umask = os.umask(0o777)
    try:
        store.save(_credentials())
    finally:
        os.umask(old_umask)

    loaded = store.load()
    assert loaded == _credentials()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE((directory / 'token.json.lock').stat().st_mode) == 0o600
    )


def test_save_completes_partial_writes(tmp_path: Path) -> None:
    store = GoogleCredentialStore(tmp_path / 'token.json')
    real_write = os.write

    def partial_write(fd: int, data: bytes | memoryview) -> int:
        """Write five bytes maximum."""
        view = memoryview(data)
        return real_write(fd, view[: min(len(view), 5)])

    with patch('os.write', side_effect=partial_write):
        store.save(_credentials(token='partial-write-token'))

    loaded = store.load()
    assert loaded is not None
    assert loaded.token == 'partial-write-token'


def test_save_rejects_zero_write_and_cleans_temp(tmp_path: Path) -> None:
    store = GoogleCredentialStore(tmp_path / 'token.json')
    with patch('os.write', return_value=0), pytest.raises(GoogleAuthError):
        store.save(_credentials())
    assert list(tmp_path.glob('.tmp_*')) == []


def test_save_orders_durability_barriers(tmp_path: Path) -> None:
    store = GoogleCredentialStore(tmp_path / 'token.json')
    order: list[str] = []
    replace_kwargs: dict[str, Any] = {}
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        """Record filesystem sync call."""
        order.append('fsync')
        real_fsync(fd)

    def spy_replace(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        """Record atomic replace call."""
        order.append('replace')
        replace_kwargs['src_dir_fd'] = src_dir_fd
        replace_kwargs['dst_dir_fd'] = dst_dir_fd
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    with (
        patch('os.fsync', side_effect=spy_fsync),
        patch('os.replace', side_effect=spy_replace),
    ):
        store.save(_credentials())

    assert order == ['fsync', 'replace', 'fsync']
    assert replace_kwargs['src_dir_fd'] is not None
    assert replace_kwargs['src_dir_fd'] == replace_kwargs['dst_dir_fd']


def test_credentials_google_round_trip() -> None:
    moments = (
        datetime(2026, 8, 24, 12, tzinfo=UTC),
        datetime(
            2026,
            8,
            24,
            12,
            0,
            0,
            123456,
            tzinfo=UTC,
        ),
        datetime(
            2026,
            8,
            24,
            15,
            tzinfo=timezone(timedelta(hours=3)),
        ),
    )
    for moment in moments:
        credentials = _credentials()
        credentials = GoogleCredentials(
            **{
                **credentials.to_dict(),
                'scopes': credentials.scopes,
                'expiry': moment,
            }
        )
        google_credentials = credentials.to_google_credentials()
        expected = moment.astimezone(
            __import__('datetime').timezone.utc
        ).replace(tzinfo=None)
        assert google_credentials.expiry == expected
        restored = GoogleCredentials.from_google_credentials(
            google_credentials
        )
        assert restored.expiry == expected.replace(
            tzinfo=__import__('datetime').timezone.utc
        )


def test_refresh_fails_on_reduced_granted_scopes(tmp_path: Path) -> None:
    required = 'https://www.googleapis.com/auth/gmail.readonly'
    server = FakeTokenServer(
        scope='https://www.googleapis.com/auth/gmail.labels'
    )
    try:
        store = GoogleCredentialStore(
            tmp_path / 'token.json',
            required_scopes=(required,),
        )
        store.save(
            _credentials(token_uri=server.token_uri, scopes=(required,))
        )
        with pytest.raises(ScopeMismatchError):
            store.refresh()
    finally:
        server.close()


def test_refresh_preserves_scopes_when_response_omits_them(
    tmp_path: Path,
) -> None:
    required = 'https://www.googleapis.com/auth/gmail.readonly'
    server = FakeTokenServer()
    try:
        store = GoogleCredentialStore(
            tmp_path / 'token.json',
            required_scopes=(required,),
        )
        store.save(
            _credentials(token_uri=server.token_uri, scopes=(required,))
        )
        refreshed = store.refresh()
        assert refreshed.scopes == (required,)
    finally:
        server.close()


def test_refresh_maps_structured_invalid_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GoogleCredentialStore(tmp_path / 'token.json')
    store.save(_credentials())

    def revoked_refresh(self: Any, request: Any) -> None:
        """Raise structured revoked error."""
        raise RefreshError(
            'display text without classification value',
            {'error': 'invalid_grant'},
            retryable=False,
        )

    monkeypatch.setattr(OAuth2Credentials, 'refresh', revoked_refresh)
    with pytest.raises(TokenRevokedError):
        store.refresh()


def test_retry_classification_and_execution() -> None:
    assert is_retryable_google_error(RefreshError('temporary', retryable=True))
    assert not is_retryable_google_error(
        RefreshError('permanent', retryable=False)
    )
    assert is_retryable_google_error(TransportError('network'))
    assert not is_retryable_google_error(ValueError('programmer error'))

    attempts = 0
    delays: list[float] = []

    def operation() -> str:
        """Fail twice then succeed."""
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransportError('temporary')
        return 'ok'

    config = RetryConfig(
        max_attempts=3,
        initial_delay=2,
        max_delay=3,
        jitter=True,
    )
    result = execute_with_retry(
        operation,
        config=config,
        sleep_fn=delays.append,
        random_fn=lambda low, high: high,
    )
    assert result == 'ok'
    assert attempts == 3
    assert delays == [2, 3]


def test_retry_stops_on_permanent_and_attempt_limit() -> None:
    permanent_attempts = 0

    def permanent() -> None:
        """Raise permanent operation error."""
        nonlocal permanent_attempts
        permanent_attempts += 1
        raise ValueError('permanent')

    with pytest.raises(ValueError):
        execute_with_retry(permanent)
    assert permanent_attempts == 1

    transient_attempts = 0

    def transient() -> None:
        """Raise transient operation error."""
        nonlocal transient_attempts
        transient_attempts += 1
        raise TransportError('temporary')

    with pytest.raises(TransportError):
        execute_with_retry(
            transient,
            config=RetryConfig(max_attempts=3, jitter=False),
            sleep_fn=lambda delay: None,
        )
    assert transient_attempts == 3


def test_store_refresh_uses_retry(tmp_path: Path, monkeypatch: Any) -> None:
    store = GoogleCredentialStore(tmp_path / 'token.json')
    store.save(_credentials())
    attempts = 0

    def transient_refresh(self: Any, request: Any) -> None:
        """Fail once then refresh."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RefreshError('temporary', retryable=True)
        self.token = 'refreshed'

    monkeypatch.setattr(OAuth2Credentials, 'refresh', transient_refresh)
    refreshed = store.refresh()
    assert refreshed.token == 'refreshed'
    assert attempts == 2


def test_multiprocess_refresh_is_single_flight(tmp_path: Path) -> None:
    server = FakeTokenServer(delay=0.1)
    token_path = tmp_path / 'token.json'
    store = GoogleCredentialStore(token_path)
    store.save(_credentials(token_uri=server.token_uri))
    gate = tmp_path / 'start-gate'
    ready_paths = [tmp_path / f'ready-{index}' for index in range(2)]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                '-c',
                _WORKER_SCRIPT,
                str(token_path),
                str(ready),
                str(gate),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ready in ready_paths
    ]
    try:
        deadline = time.monotonic() + 5
        while not all(path.exists() for path in ready_paths):
            if time.monotonic() >= deadline:
                raise TimeoutError('worker readiness timeout')
            if any(process.poll() is not None for process in processes):
                raise RuntimeError('worker exited before barrier')
            time.sleep(0.01)
        gate.touch()

        outputs: list[str] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == 0, stderr
            outputs.append(stdout.strip())

        assert outputs == [
            'refreshed-token-payload',
            'refreshed-token-payload',
        ]
        assert server.request_count == 1
        loaded = store.load()
        assert loaded is not None
        assert loaded.token == 'refreshed-token-payload'
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)
        server.close()


def test_five_service_stores_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stores: dict[str, GoogleCredentialStore] = {}
    for service in ('gmail', 'calendar', 'drive', 'sheets', 'docs'):
        root = tmp_path / service
        root.mkdir(mode=0o700)
        prefix = service.upper()
        monkeypatch.setenv(f'{prefix}_MCP_PORT', '8999')
        monkeypatch.setenv(
            f'{prefix}_MCP_PUBLIC_URL',
            f'https://mcp.example.com/{service}',
        )
        monkeypatch.setenv(f'{prefix}_MCP_PATH', f'/{service}/mcp')
        monkeypatch.setenv(
            f'{prefix}_MCP_DOWNLOAD_PATH', str(root / 'downloads')
        )
        monkeypatch.setenv(
            f'{prefix}_OAUTH_STATE_PATH', str(root / 'oauth.sqlite3')
        )
        monkeypatch.setenv(
            f'{prefix}_GOOGLE_TOKEN_PATH', str(root / 'token.json')
        )
        monkeypatch.setenv(
            f'{prefix}_AUDIT_LOG_PATH', str(root / 'audit.jsonl')
        )
        monkeypatch.setenv(f'{prefix}_OAUTH_LOGIN_USERNAME', 'admin')
        monkeypatch.setenv(f'{prefix}_OAUTH_LOGIN_PASSWORD', 'password')
        config = ServiceConfig.from_env(service)
        store = GoogleCredentialStore(
            config.google_token_path,
            config.download_path,
        )
        store.save(_credentials(token=f'{service}-token'))
        stores[service] = store

    assert len({store.path for store in stores.values()}) == 5
    for service, store in stores.items():
        loaded = store.load()
        assert loaded is not None
        assert loaded.token == f'{service}-token'


def test_secrets_do_not_escape_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = 'SYNTH_SECRET_XY78'
    refresh_secret = 'SYNTH_REFRESH_QZ91'
    path_secret = 'SYNTH_PATH_ZZ33'
    cause_secret = 'SYNTH_CAUSE_AA11'
    scope_secret = 'SYNTH_SCOPE_BB22'
    credentials = GoogleCredentials(
        token=secret,
        refresh_token=refresh_secret,
        client_secret=secret,
    )
    assert secret not in repr(credentials)
    assert refresh_secret not in repr(credentials)

    bad_path = tmp_path / path_secret / 'token.json'
    with pytest.raises(UnsafeCredentialPath) as path_error:
        validate_credential_path(bad_path, bad_path.parent)
    path_traceback = ''.join(traceback.format_exception(path_error.value))
    assert path_secret not in str(path_error.value)
    assert path_secret not in path_traceback

    store = GoogleCredentialStore(tmp_path / 'token.json')
    with (
        patch('os.write', side_effect=OSError(cause_secret)),
        pytest.raises(GoogleAuthError) as save_error,
    ):
        store.save(credentials)
    save_traceback = ''.join(traceback.format_exception(save_error.value))
    assert cause_secret not in str(save_error.value)
    assert cause_secret not in save_traceback

    store.save(credentials)

    def leaking_refresh(self: Any, request: Any) -> None:
        """Raise provider payload marker."""
        raise RefreshError(
            'provider failure',
            {
                'error': 'invalid_grant',
                'error_description': refresh_secret,
            },
            retryable=False,
        )

    monkeypatch.setattr(OAuth2Credentials, 'refresh', leaking_refresh)
    with pytest.raises(TokenRevokedError) as refresh_error:
        store.refresh()
    refresh_traceback = ''.join(
        traceback.format_exception(refresh_error.value)
    )
    assert refresh_secret not in str(refresh_error.value)
    assert refresh_secret not in refresh_traceback

    scoped = GoogleCredentialStore(
        tmp_path / 'scoped.json',
        required_scopes=(scope_secret,),
    )
    scoped.save(_credentials(scopes=('other-scope',)))
    with pytest.raises(ScopeMismatchError) as scope_error:
        scoped.get_credentials()
    scope_traceback = ''.join(traceback.format_exception(scope_error.value))
    assert scope_secret not in str(scope_error.value)
    assert scope_secret not in scope_traceback

    for marker in (
        secret,
        refresh_secret,
        path_secret,
        cause_secret,
        scope_secret,
    ):
        assert marker not in caplog.text
