"""Test Google authorization entrypoint."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.cli.authorize import SERVICE_SCOPES, main
from google_workspace_mcp.google_auth import GoogleCredentials
from google_workspace_mcp.google_auth.errors import ScopeMismatchError
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
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    code, out, _ = run_cli(workspace, 'docs', consent)
    token_path = workspace / 'docs-token.json'
    stored = json.loads(token_path.read_text())
    assert code == 0
    assert stored['refresh_token'] == 'refresh-value'
    assert json.loads(out)['service'] == 'docs'


def test_token_file_is_owner_only(workspace: Path) -> None:
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    run_cli(workspace, 'docs', consent)
    mode = (workspace / 'docs-token.json').stat().st_mode & 0o777
    assert mode == 0o600


def test_consent_receives_service_scopes(workspace: Path) -> None:
    seen: dict[str, Any] = {}

    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Record requested consent scopes."""
        seen['secrets'] = secrets
        seen['requested'] = requested
        return credentials_for(CALENDAR_SCOPES)

    code, _, _ = run_cli(workspace, 'calendar', consent)
    assert code == 0
    assert seen['requested'] == CALENDAR_SCOPES
    assert seen['secrets'] == workspace / 'client_secret.json'


def test_summary_never_prints_secret_values(workspace: Path) -> None:
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    code, out, err = run_cli(workspace, 'docs', consent)
    combined = out + err
    assert code == 0
    assert 'refresh-value' not in combined
    assert 'access-value' not in combined
    assert 'client-secret-value' not in combined


def test_summary_reports_verifiable_facts(workspace: Path) -> None:
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Return granted fake credentials."""
        return credentials_for(DOCS_SCOPES)

    _, out, _ = run_cli(workspace, 'docs', consent)
    payload = json.loads(out)
    assert payload['service'] == 'docs'
    assert payload['granted_scopes'] == list(DOCS_SCOPES)
    assert payload['refresh_token_present'] is True
    assert payload['expiry'] == '2026-08-26T12:00:00+00:00'


def test_reduced_grant_writes_no_token(workspace: Path) -> None:
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Raise reduced scope failure."""
        raise ScopeMismatchError('consent granted fewer scopes than required')

    code, _, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert not (workspace / 'docs-token.json').exists()
    assert 'error' in json.loads(err)


def test_consent_failure_reports_without_traceback(workspace: Path) -> None:
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Raise transport style failure."""
        raise OSError('connection to accounts.google.com refused')

    code, out, err = run_cli(workspace, 'docs', consent)
    assert code == 1
    assert out == ''
    assert 'Traceback' not in err


def test_unknown_service_is_rejected(workspace: Path) -> None:
    def consent(secrets: Path, requested: tuple[str, ...]) -> Any:
        """Fail if consent runs."""
        raise AssertionError('consent must not run')

    with pytest.raises(SystemExit):
        run_cli(workspace, 'contacts', consent)
