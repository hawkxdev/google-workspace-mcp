"""OAuth administration CLI contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from io import StringIO
from pathlib import Path

import pytest

from google_workspace_mcp.auth.state import MCP_READONLY_V1, OAuthState
from google_workspace_mcp.cli import oauth_admin
from google_workspace_mcp.common.config import ServiceConfig

REDIRECT = 'https://client.example.test/callback'
VERIFIER = 'pkce-verifier-marker-long-enough'
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b'=')
    .decode()
)


def _configure_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    *,
    state_path: Path | None = None,
    legacy_path: Path | None = None,
) -> ServiceConfig:
    """Configure isolated service settings."""
    prefix = service.upper()
    root = tmp_path / service
    monkeypatch.setenv(
        f'{prefix}_MCP_PUBLIC_URL',
        f'https://mcp.example.test/{service}/mcp',
    )
    monkeypatch.setenv(f'{prefix}_MCP_DOWNLOAD_PATH', str(root / 'downloads'))
    monkeypatch.setenv(
        f'{prefix}_OAUTH_STATE_PATH',
        str(state_path or root / 'oauth.sqlite3'),
    )
    monkeypatch.setenv(
        f'{prefix}_GOOGLE_TOKEN_PATH', str(root / 'google-token.json')
    )
    if legacy_path is not None:
        monkeypatch.setenv(
            f'{prefix}_OAUTH_LEGACY_CLIENTS_PATH', str(legacy_path)
        )
    return ServiceConfig.from_env(service)


def _state(config: ServiceConfig) -> OAuthState:
    """Open configured OAuth state."""
    return OAuthState(
        config.oauth_state_path,
        download_path=config.download_path,
        service_id=config.service_id,
        resource=config.public_url,
        readonly_capabilities=(f'{config.service_id}.read',),
        legacy_path=config.legacy_clients_path,
        approved_legacy_client_ids=config.approved_legacy_client_ids,
        access_token_ttl_seconds=config.access_token_ttl_seconds,
        refresh_token_ttl_seconds=config.refresh_token_ttl_seconds,
    )


def _run(service: str, *command: str) -> tuple[int, str, str]:
    """Run captured admin command."""
    stdout = StringIO()
    stderr = StringIO()
    result = oauth_admin.main(
        ['--service', service, *command],
        stdout=stdout,
        stderr=stderr,
    )
    return result, stdout.getvalue(), stderr.getvalue()


def test_service_selection_is_required() -> None:
    with pytest.raises(SystemExit, match='2'):
        oauth_admin.main(['clients', 'list'])


def test_client_inventory_and_revoke_are_metadata_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _configure_service(monkeypatch, tmp_path, 'gmail')
    state = _state(config)
    registered = state.register_client([REDIRECT], client_name='CLI test')
    authorization_code = state.issue_authorization_code(
        client_id=registered.client.client_id,
        redirect_uri=REDIRECT,
        code_challenge='challenge',
        resource=config.public_url,
    )
    state.close()

    result, stdout, stderr = _run('gmail', 'clients', 'list')
    payload = json.loads(stdout)

    assert result == 0
    assert stderr == ''
    assert payload == [
        {
            'client_id': registered.client.client_id,
            'client_name': 'CLI test',
            'created_at': registered.client.created_at,
            'is_static': False,
            'last_authorized_at': None,
            'policy': MCP_READONLY_V1,
            'redirect_uris': [REDIRECT],
            'revoked_at': None,
        }
    ]
    captured = stdout + stderr
    assert registered.client_secret not in captured
    assert authorization_code not in captured

    result, stdout, stderr = _run(
        'gmail',
        'clients',
        'revoke',
        registered.client.client_id,
    )

    assert result == 0
    assert stderr == ''
    assert json.loads(stdout) == {
        'client_id': registered.client.client_id,
        'revoked': True,
    }
    with _state(config) as reopened:
        client = reopened.get_client(registered.client.client_id)
        assert client is not None
        assert client.revoked_at is not None


def test_token_inventory_filter_and_revoke_never_expose_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _configure_service(monkeypatch, tmp_path, 'drive')
    state = _state(config)
    first = state.register_client([REDIRECT])
    second = state.register_client(['https://second.example.test/callback'])
    first_token = state.issue_access_token(
        client_id=first.client.client_id,
        resource=config.public_url,
    )
    state.issue_access_token(
        client_id=second.client.client_id,
        resource=config.public_url,
    )
    state.close()

    result, stdout, stderr = _run(
        'drive',
        'tokens',
        'list',
        '--client-id',
        first.client.client_id,
    )
    payload = json.loads(stdout)

    assert result == 0
    assert stderr == ''
    assert len(payload) == 1
    assert payload[0]['token_id'] == first_token.token.token_id
    assert payload[0]['capabilities'] == ['drive.read']
    captured = stdout + stderr
    assert first_token.access_token not in captured
    assert first_token.access_token.split('.')[-1] not in captured

    result, stdout, stderr = _run(
        'drive', 'tokens', 'revoke', first_token.token.token_id
    )

    assert result == 0
    assert stderr == ''
    assert json.loads(stdout) == {
        'revoked': True,
        'token_id': first_token.token.token_id,
    }


def test_backup_is_online_reopenable_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _configure_service(monkeypatch, tmp_path, 'calendar')
    state = _state(config)
    registered = state.register_client([REDIRECT])
    authorization_code = state.issue_authorization_code(
        client_id=registered.client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        resource=config.public_url,
    )
    issued = state.redeem_authorization_code(
        code=authorization_code,
        client_id=registered.client.client_id,
        client_secret=registered.client_secret,
        redirect_uri=REDIRECT,
        code_verifier=VERIFIER,
        resource=config.public_url,
    )
    state.close()
    backup = tmp_path / 'backup' / 'oauth.sqlite3'

    result, stdout, stderr = _run('calendar', 'backup', str(backup))

    assert result == 0
    assert stderr == ''
    assert json.loads(stdout) == {'backup': str(backup)}
    with sqlite3.connect(backup) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        assert connection.execute(
            'SELECT count(*) FROM clients'
        ).fetchone() == (1,)
        assert connection.execute(
            'SELECT count(*) FROM access_tokens'
        ).fetchone() == (1,)
    backup_bytes = backup.read_bytes()
    assert registered.client_secret.encode() not in backup_bytes
    assert authorization_code.encode() not in backup_bytes
    assert issued.access_token.encode() not in backup_bytes
    assert issued.refresh_token is not None
    assert issued.refresh_token.encode() not in backup_bytes


def test_metadata_command_does_not_consume_pending_legacy_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy_path = tmp_path / 'legacy' / 'clients.json'
    legacy_path.parent.mkdir(mode=0o700)
    legacy_path.write_text(
        json.dumps(
            {
                'pending-client': {
                    'client_secret': 'pending-secret',
                    'redirect_uris': [REDIRECT],
                }
            }
        )
    )
    legacy_path.chmod(0o600)
    config = _configure_service(
        monkeypatch,
        tmp_path,
        'sheets',
        legacy_path=legacy_path,
    )

    result, stdout, stderr = _run('sheets', 'clients', 'list')

    assert result == 0
    assert stderr == ''
    assert json.loads(stdout) == []
    assert 'pending-secret' not in stdout + stderr
    assert legacy_path.exists()
    with sqlite3.connect(config.oauth_state_path) as connection:
        assert connection.execute(
            'SELECT count(*) FROM migration_metadata'
        ).fetchone() == (0,)


def test_existing_ownerless_state_is_refused_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _configure_service(monkeypatch, tmp_path, 'docs')
    with _state(config):
        pass
    with sqlite3.connect(config.oauth_state_path) as connection:
        connection.execute('DROP TABLE state_owner')
    original_bytes = config.oauth_state_path.read_bytes()

    result, stdout, stderr = _run('docs', 'clients', 'list')

    assert result == 1
    assert stdout == ''
    assert 'no owner metadata' in stderr
    assert config.oauth_state_path.read_bytes() == original_bytes


def test_failed_client_revoke_does_not_reflect_client_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _configure_service(monkeypatch, tmp_path, 'gmail')
    with _state(config) as state:
        registered = state.register_client([REDIRECT])

    result, stdout, stderr = _run(
        'gmail', 'clients', 'revoke', registered.client_secret
    )

    assert result == 1
    assert registered.client_secret not in stdout + stderr
    assert json.loads(stdout) == {'revoked': False}


def test_failed_token_revoke_does_not_reflect_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _configure_service(monkeypatch, tmp_path, 'drive')
    with _state(config) as state:
        registered = state.register_client([REDIRECT])
        issued = state.issue_access_token(
            client_id=registered.client.client_id,
            resource=config.public_url,
        )

    result, stdout, stderr = _run(
        'drive', 'tokens', 'revoke', issued.access_token
    )

    assert result == 1
    assert issued.access_token not in stdout + stderr
    assert json.loads(stdout) == {'revoked': False}


def test_swapped_service_path_is_refused_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gmail = _configure_service(monkeypatch, tmp_path, 'gmail')
    drive = _configure_service(monkeypatch, tmp_path, 'drive')
    with _state(gmail) as gmail_state:
        gmail_client = gmail_state.register_client([REDIRECT]).client
    with _state(drive) as drive_state:
        drive_client = drive_state.register_client(
            ['https://drive-client.example.test/callback']
        ).client

    monkeypatch.setenv('GMAIL_OAUTH_STATE_PATH', str(drive.oauth_state_path))
    result, stdout, stderr = _run(
        'gmail', 'clients', 'revoke', drive_client.client_id
    )

    assert result == 1
    assert stdout == ''
    assert 'state belongs to service drive' in stderr
    with _state(gmail) as fresh_gmail:
        assert fresh_gmail.list_clients() == (gmail_client,)
    with _state(drive) as fresh_drive:
        assert fresh_drive.list_clients() == (drive_client,)
