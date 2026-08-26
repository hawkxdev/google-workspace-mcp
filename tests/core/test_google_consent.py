"""Test Google consent flow."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from google_workspace_mcp.google_auth.consent import (
    CONSENT_PROMPT,
    OFFLINE_ACCESS_TYPE,
    run_consent_flow,
)
from google_workspace_mcp.google_auth.errors import (
    GoogleAuthError,
    ScopeMismatchError,
    UnsafeCredentialPath,
)

SCOPES = (
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive.file',
)


class FakeLibraryCredentials:
    """Provide fake library credentials."""

    def __init__(
        self,
        refresh_token: str | None = 'refresh-value',
        granted_scopes: list[str] | None = None,
    ) -> None:
        """Initialize fake library credentials."""
        self.token = 'access-value'
        self.refresh_token = refresh_token
        self.token_uri = 'https://oauth2.googleapis.com/token'
        self.client_id = 'client-id-value'
        self.client_secret = 'client-secret-value'
        self.scopes = list(SCOPES)
        self.granted_scopes = (
            list(SCOPES) if granted_scopes is None else granted_scopes
        )
        self.expiry = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class FakeFlow:
    """Record consent flow invocation."""

    def __init__(
        self,
        credentials: FakeLibraryCredentials | None = None,
    ) -> None:
        """Initialize fake consent flow."""
        self.calls: list[dict[str, Any]] = []
        self.credentials = credentials or FakeLibraryCredentials()

    def run_local_server(self, **kwargs: Any) -> FakeLibraryCredentials:
        """Record local server invocation."""
        self.calls.append(dict(kwargs))
        return self.credentials


class FakeFactory:
    """Record flow construction arguments."""

    def __init__(self, flow: FakeFlow) -> None:
        """Initialize fake flow factory."""
        self.flow = flow
        self.calls: list[tuple[Any, tuple[str, ...]]] = []

    def __call__(self, client_config: Any, scopes: Any) -> FakeFlow:
        """Record flow factory call."""
        self.calls.append((client_config, tuple(scopes)))
        return self.flow


@pytest.fixture
def client_secrets(tmp_path: Path) -> Path:
    """Create owner only secrets."""
    root = tmp_path / 'secrets'
    root.mkdir(mode=0o700)
    path = root / 'client_secret.json'
    path.write_text(
        json.dumps({'installed': {'client_id': 'client-id-value'}})
    )
    path.chmod(0o600)
    return path


def test_flow_requests_offline_access(client_secrets: Path) -> None:
    flow = FakeFlow()
    factory = FakeFactory(flow)
    run_consent_flow(client_secrets, SCOPES, flow_factory=factory)
    assert flow.calls[0]['access_type'] == OFFLINE_ACCESS_TYPE
    assert flow.calls[0]['access_type'] == 'offline'


def test_flow_forces_consent_prompt(client_secrets: Path) -> None:
    flow = FakeFlow()
    run_consent_flow(client_secrets, SCOPES, flow_factory=FakeFactory(flow))
    assert flow.calls[0]['prompt'] == CONSENT_PROMPT
    assert flow.calls[0]['prompt'] == 'consent'


def test_flow_binds_to_loopback(client_secrets: Path) -> None:
    flow = FakeFlow()
    run_consent_flow(client_secrets, SCOPES, flow_factory=FakeFactory(flow))
    assert flow.calls[0]['host'] == '127.0.0.1'


def test_flow_receives_requested_scopes(client_secrets: Path) -> None:
    flow = FakeFlow()
    factory = FakeFactory(flow)
    run_consent_flow(client_secrets, SCOPES, flow_factory=factory)
    assert factory.calls[0][1] == SCOPES
    assert factory.calls[0][0] == {
        'installed': {'client_id': 'client-id-value'}
    }


def test_flow_returns_project_credentials(client_secrets: Path) -> None:
    flow = FakeFlow()
    credentials = run_consent_flow(
        client_secrets, SCOPES, flow_factory=FakeFactory(flow)
    )
    assert credentials.refresh_token == 'refresh-value'
    assert credentials.scopes == SCOPES
    assert credentials.expiry == datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_missing_refresh_token_fails_closed(client_secrets: Path) -> None:
    flow = FakeFlow(FakeLibraryCredentials(refresh_token=None))
    with pytest.raises(GoogleAuthError, match='refresh token'):
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )


def test_empty_refresh_token_fails_closed(client_secrets: Path) -> None:
    flow = FakeFlow(FakeLibraryCredentials(refresh_token=''))
    with pytest.raises(GoogleAuthError, match='refresh token'):
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )


def test_reduced_grant_fails_closed(client_secrets: Path) -> None:
    flow = FakeFlow(
        FakeLibraryCredentials(
            granted_scopes=['https://www.googleapis.com/auth/drive.file']
        )
    )
    with pytest.raises(ScopeMismatchError):
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )


def test_extra_granted_scope_is_accepted(client_secrets: Path) -> None:
    granted = [*SCOPES, 'https://www.googleapis.com/auth/userinfo.email']
    flow = FakeFlow(FakeLibraryCredentials(granted_scopes=granted))
    credentials = run_consent_flow(
        client_secrets, SCOPES, flow_factory=FakeFactory(flow)
    )
    assert set(SCOPES).issubset(credentials.scopes)


def test_missing_secrets_file_is_rejected(tmp_path: Path) -> None:
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath):
        run_consent_flow(
            tmp_path / 'absent.json', SCOPES, flow_factory=FakeFactory(flow)
        )
    assert flow.calls == []


def test_world_readable_secrets_file_is_rejected(
    client_secrets: Path,
) -> None:
    client_secrets.chmod(0o644)
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath):
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )
    assert flow.calls == []


def test_symlinked_secrets_file_is_rejected(
    tmp_path: Path, client_secrets: Path
) -> None:
    link = tmp_path / 'link.json'
    link.symlink_to(client_secrets)
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath):
        run_consent_flow(link, SCOPES, flow_factory=FakeFactory(flow))
    assert flow.calls == []


def test_directory_as_secrets_file_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / 'secrets_dir'
    directory.mkdir(mode=0o700)
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath):
        run_consent_flow(directory, SCOPES, flow_factory=FakeFactory(flow))
    assert flow.calls == []


def test_empty_scopes_are_rejected(client_secrets: Path) -> None:
    flow = FakeFlow()
    with pytest.raises(GoogleAuthError, match='at least one scope'):
        run_consent_flow(client_secrets, (), flow_factory=FakeFactory(flow))
    assert flow.calls == []


def test_errors_never_expose_secret_values(client_secrets: Path) -> None:
    flow = FakeFlow(FakeLibraryCredentials(refresh_token=None))
    with pytest.raises(GoogleAuthError) as caught:
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )
    message = str(caught.value)
    assert 'client-secret-value' not in message
    assert 'access-value' not in message
    assert str(client_secrets) not in message


def test_secrets_file_owned_by_other_user_is_rejected(
    client_secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = os.fstat
    target_inode = os.stat(client_secrets).st_ino

    def fake_fstat(fd: int) -> Any:
        """Report foreign target owner."""
        result = real_fstat(fd)
        if result.st_ino == target_inode:

            class Foreign:
                """Expose foreign ownership metadata."""

                st_mode = result.st_mode
                st_uid = result.st_uid + 1

            return Foreign()
        return result

    monkeypatch.setattr(
        'google_workspace_mcp.google_auth.consent.os.fstat', fake_fstat
    )
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath):
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )
    assert flow.calls == []


def test_symlinked_ancestor_directory_is_rejected(
    tmp_path: Path, client_secrets: Path
) -> None:
    linked_root = tmp_path / 'linked'
    linked_root.symlink_to(client_secrets.parent)
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath):
        run_consent_flow(
            linked_root / client_secrets.name,
            SCOPES,
            flow_factory=FakeFactory(flow),
        )
    assert flow.calls == []


def test_flow_factory_never_receives_a_path(client_secrets: Path) -> None:
    flow = FakeFlow()
    factory = FakeFactory(flow)
    run_consent_flow(client_secrets, SCOPES, flow_factory=factory)
    passed = factory.calls[0][0]
    assert isinstance(passed, dict)
    assert str(client_secrets) not in repr(passed)


def test_library_output_never_reaches_stdout(
    client_secrets: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class ChattyFlow(FakeFlow):
        """Print an authorization URL like the library."""

        def run_local_server(self, **kwargs: Any) -> FakeLibraryCredentials:
            """Print prompt then return credentials."""
            print('Please visit this URL to authorize: https://accounts...')
            return super().run_local_server(**kwargs)

    flow = ChattyFlow()
    run_consent_flow(client_secrets, SCOPES, flow_factory=FakeFactory(flow))
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Please visit this URL' in captured.err


def test_client_secrets_over_size_limit_is_rejected(
    client_secrets: Path,
) -> None:
    payload = {'installed': {'client_id': 'x' * (64 * 1024)}}
    client_secrets.write_text(json.dumps(payload))
    client_secrets.chmod(0o600)
    flow = FakeFlow()
    with pytest.raises(UnsafeCredentialPath, match='too large'):
        run_consent_flow(
            client_secrets, SCOPES, flow_factory=FakeFactory(flow)
        )
    assert flow.calls == []
