"""Test Google authorization entrypoint."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.cli.authorize import SERVICE_SCOPES, main
from google_workspace_mcp.google_auth import GoogleCredentials
from google_workspace_mcp.google_auth.errors import (
    GoogleAuthError,
    ScopeMismatchError,
)
from google_workspace_mcp.services.calendar.constants import CALENDAR_SCOPES
from google_workspace_mcp.services.docs.constants import DOCS_SCOPES
from google_workspace_mcp.services.drive.constants import DRIVE_SCOPES
from google_workspace_mcp.services.gmail.constants import GMAIL_SCOPE
from google_workspace_mcp.services.sheets.constants import SHEETS_SCOPES


def credentials_for(scopes: tuple[str, ...]) -> GoogleCredentials:
    """Build fake granted credentials."""
    return GoogleCredentials(
        token='access-value',
        refresh_token='refresh-value',
        client_id='client-id-value',
        client_secret='client-secret-value',
        scopes=scopes,
        expiry=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create owner only workspace."""
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    (root / 'downloads').mkdir(mode=0o700)
    secrets = root / 'client_secret.json'
    secrets.write_text(json.dumps({'installed': {'client_id': 'value'}}))
    secrets.chmod(0o600)
    return root


def run_cli(
    workspace: Path,
    service: str,
    consent: Any,
    extra: list[str] | None = None,
) -> tuple[int, str, str]:
    """Run authorization entrypoint locally."""
    out = io.StringIO()
    err = io.StringIO()
    argv = [
        '--service',
        service,
        '--client-secrets',
        str(workspace / 'client_secret.json'),
        '--token-path',
        str(workspace / f'{service}-token.json'),
        '--download-path',
        str(workspace / 'downloads'),
        *(extra or []),
    ]
    code = main(argv, out=out, errors=err, consent_runner=consent)
    return code, out.getvalue(), err.getvalue()


def test_service_scopes_cover_every_service() -> None:
    assert set(SERVICE_SCOPES) == set(SERVICES)


def test_service_scopes_match_service_constants() -> None:
    assert SERVICE_SCOPES['gmail'] == (GMAIL_SCOPE,)
    assert SERVICE_SCOPES['calendar'] == CALENDAR_SCOPES
    assert SERVICE_SCOPES['drive'] == DRIVE_SCOPES
    assert SERVICE_SCOPES['sheets'] == SHEETS_SCOPES
    assert SERVICE_SCOPES['docs'] == DOCS_SCOPES


def test_distinct_scope_inventory_is_eight() -> None:
    distinct = {
        scope for scopes in SERVICE_SCOPES.values() for scope in scopes
    }
    assert len(distinct) == 8


def test_every_service_keeps_a_separate_token_file(workspace: Path) -> None:
    recorded: list[Path] = []

    for service in SERVICES:
        scopes = SERVICE_SCOPES[service]

        def consent(
            secrets: Path,
            requested: tuple[str, ...],
            port: int = 0,
            granted: tuple[str, ...] = scopes,
        ) -> GoogleCredentials:
            """Return granted fake credentials."""
            return credentials_for(granted)

        code, _, _ = run_cli(workspace, service, consent)
        assert code == 0
        recorded.append(workspace / f'{service}-token.json')

    assert len({path.read_text() for path in recorded}) >= 1
    assert all(path.exists() for path in recorded)
    assert len(set(recorded)) == 5


def test_credentials_are_persisted_through_store(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    code, out, _ = run_cli(workspace, 'docs', consent)
    token_path = workspace / 'docs-token.json'
    stored = json.loads(token_path.read_text())
    assert code == 0
    assert stored['refresh_token'] == 'refresh-value'
    assert json.loads(out)['service'] == 'docs'


def test_token_file_is_owner_only(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    run_cli(workspace, 'docs', consent)
    mode = (workspace / 'docs-token.json').stat().st_mode & 0o777
    assert mode == 0o600


def test_consent_receives_service_scopes(workspace: Path) -> None:
    seen: dict[str, Any] = {}

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Record requested consent scopes."""
        seen['secrets'] = secrets
        seen['requested'] = requested
        return credentials_for(CALENDAR_SCOPES)

    code, _, _ = run_cli(workspace, 'calendar', consent)
    assert code == 0
    assert seen['requested'] == CALENDAR_SCOPES
    assert seen['secrets'] == workspace / 'client_secret.json'


def test_summary_never_prints_secret_values(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    code, out, err = run_cli(workspace, 'docs', consent)
    combined = out + err
    assert code == 0
    assert 'refresh-value' not in combined
    assert 'access-value' not in combined
    assert 'client-secret-value' not in combined


def test_summary_reports_verifiable_facts(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    _, out, _ = run_cli(workspace, 'docs', consent)
    payload = json.loads(out)
    assert payload['service'] == 'docs'
    assert payload['granted_scopes'] == list(DOCS_SCOPES)
    assert payload['refresh_token_present'] is True
    assert payload['expiry'] == '2026-08-26T12:00:00+00:00'


def test_reduced_grant_writes_no_token(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise reduced scope failure."""
        raise ScopeMismatchError('consent granted fewer scopes than required')

    code, _, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert not (workspace / 'docs-token.json').exists()
    assert 'error' in json.loads(err)


def test_consent_failure_reports_without_traceback(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise transport style failure."""
        raise OSError('connection to accounts.google.com refused')

    code, out, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert out == ''
    assert 'Traceback' not in err


def test_unknown_service_is_rejected(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Fail if consent runs."""
        raise AssertionError('consent must not run')

    with pytest.raises(SystemExit):
        run_cli(workspace, 'contacts', consent)


def test_documented_command_runs_without_service_environment(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in list(os.environ):
        if '_MCP_' in key or key.endswith('_GOOGLE_TOKEN_PATH'):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        'google_workspace_mcp.common.config._STATE_ROOT', workspace / 'state'
    )
    seen: dict[str, Any] = {}

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Record that consent was reached."""
        seen['reached'] = True
        return credentials_for(DOCS_SCOPES)

    out = io.StringIO()
    err = io.StringIO()
    code = main(
        [
            '--service',
            'docs',
            '--client-secrets',
            str(workspace / 'client_secret.json'),
        ],
        out=out,
        errors=err,
        consent_runner=consent,
    )
    assert code == 0
    assert seen.get('reached') is True
    assert json.loads(out.getvalue())['token_path'].endswith(
        'google_token.json'
    )


def test_unusable_token_path_fails_before_consent(workspace: Path) -> None:
    calls: list[Path] = []

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Record that consent was reached."""
        calls.append(secrets)
        return credentials_for(DOCS_SCOPES)

    blocked = workspace / 'blocked'
    blocked.mkdir(mode=0o755)
    out = io.StringIO()
    err = io.StringIO()
    code = main(
        [
            '--service',
            'docs',
            '--client-secrets',
            str(workspace / 'client_secret.json'),
            '--token-path',
            str(blocked / 'token.json'),
            '--download-path',
            str(workspace / 'downloads'),
        ],
        out=out,
        errors=err,
        consent_runner=consent,
    )
    assert calls == []
    assert code == 1
    assert out.getvalue() == ''
    assert not (blocked / 'token.json').exists()


def test_unknown_failure_message_carries_no_payload(
    workspace: Path,
) -> None:
    class OAuthLibStyle(Exception):
        """Represent a foreign library failure."""

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise a foreign library failure."""
        raise OAuthLibStyle(
            '(mismatching_state) authorization_response=code=SECRET123'
        )

    code, out, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert out == ''
    assert 'SECRET123' not in err
    assert 'authorization_response' not in err


def test_os_error_message_hides_owner_path(workspace: Path) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise a filesystem failure naming a path."""
        raise FileNotFoundError(
            2, 'No such file or directory', '/Users/owner/secrets/value.json'
        )

    code, _, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert '/Users/owner/secrets/value.json' not in err
    assert 'No such file or directory' in json.loads(err)['error']


def test_port_argument_reaches_consent_runner(workspace: Path) -> None:
    seen: dict[str, Any] = {}

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Record the forwarded port."""
        seen['port'] = port
        return credentials_for(DOCS_SCOPES)

    code, _, _ = run_cli(workspace, 'docs', consent, extra=['--port', '8765'])
    assert code == 0
    assert seen['port'] == 8765


def test_os_error_message_carries_only_the_error_name(
    workspace: Path,
) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise a filesystem failure carrying a payload."""
        raise OSError(5, 'authorization_response=code=SECRET123')

    code, _, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert 'SECRET123' not in err
    assert 'authorization_response' not in err
    assert json.loads(err)['error'] == (
        f'credential path is unusable: {os.strerror(5)}'
    )


def test_non_integer_errno_still_yields_one_safe_json_line(
    workspace: Path,
) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise a two argument OSError with a str errno."""
        raise OSError('authorization_response=code=SECRET123', 'payload')

    code, out, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert out == ''
    assert json.loads(err) == {
        'error': 'credential path is unusable: os error'
    }
    assert 'SECRET123' not in err


def test_failing_exception_repr_cannot_escape_the_handler(
    workspace: Path,
) -> None:
    class HostileAuthError(GoogleAuthError):
        """Raise while being rendered."""

        def __str__(self) -> str:
            """Fail during rendering."""
            raise RuntimeError('token=SECRET999')

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise an error that breaks while rendering."""
        raise HostileAuthError

    code, out, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert out == ''
    assert json.loads(err) == {
        'error': 'authorization failed before credentials were stored'
    }
    assert 'SECRET999' not in err


def test_boolean_errno_is_not_read_as_a_permission_code(
    workspace: Path,
) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Raise an OSError whose errno is True."""
        error = OSError('failure')
        error.errno = True
        raise error

    code, _, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert json.loads(err)['error'] == (
        'credential path is unusable: os error'
    )
    assert os.strerror(1) not in err


def test_explicit_token_path_ignores_its_environment_variable(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('DOCS_GOOGLE_TOKEN_PATH', '   ')
    reached: list[Path] = []

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Record that consent was reached."""
        reached.append(secrets)
        return credentials_for(DOCS_SCOPES)

    code, _, _ = run_cli(workspace, 'docs', consent)
    assert code == 0
    assert reached


@pytest.mark.parametrize('port', ['-1', '65536', 'abc'])
def test_out_of_range_port_is_refused_by_the_parser(
    workspace: Path, port: str
) -> None:
    def consent(
        secrets: Path, requested: tuple[str, ...], port_value: int = 0
    ) -> Any:
        """Fail if consent runs."""
        raise AssertionError('consent must not run')

    with pytest.raises(SystemExit):
        run_cli(workspace, 'docs', consent, extra=['--port', port])


def test_missing_secrets_leaves_no_state_behind(tmp_path: Path) -> None:
    root = tmp_path / 'fresh'
    calls: list[Path] = []

    def consent(
        secrets: Path, requested: tuple[str, ...], port: int = 0
    ) -> Any:
        """Record that consent was reached."""
        calls.append(secrets)
        return credentials_for(DOCS_SCOPES)

    out = io.StringIO()
    err = io.StringIO()
    code = main(
        [
            '--service',
            'docs',
            '--client-secrets',
            str(root / 'absent.json'),
            '--token-path',
            str(root / 'state' / 'token.json'),
            '--download-path',
            str(root / 'state' / 'downloads'),
        ],
        out=out,
        errors=err,
        consent_runner=consent,
    )
    assert code == 1
    assert calls == []
    assert not root.exists()
