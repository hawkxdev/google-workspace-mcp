"""Test evaluation OAuth storage."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from google_workspace_mcp.evals.oauth_storage import FileTokenStorage

# === Fixtures ===


@pytest.fixture
def oauth_path(tmp_path: Path) -> Path:
    """Provide one OAuth state path."""
    return tmp_path / 'oauth' / 'gmail.json'


def _tokens() -> OAuthToken:
    """Build synthetic OAuth tokens."""
    return OAuthToken(
        access_token='synthetic-access-value',
        refresh_token='synthetic-refresh-value',
        expires_in=3600,
        scope='mcp_readonly_v1',
    )


def _client_info() -> OAuthClientInformationFull:
    """Build synthetic client metadata."""
    return OAuthClientInformationFull(
        client_id='synthetic-client-id',
        client_secret='synthetic-client-secret',
        redirect_uris=['http://127.0.0.1:43123/callback'],
        scope='mcp_readonly_v1',
    )


# === Storage contract ===


@pytest.mark.asyncio
async def test_storage_persists_tokens_and_client_info(
    oauth_path: Path,
) -> None:
    storage = FileTokenStorage(oauth_path)

    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None
    await storage.set_tokens(_tokens())
    await storage.set_client_info(_client_info())

    reloaded = FileTokenStorage(oauth_path)
    assert await reloaded.get_tokens() == _tokens()
    assert await reloaded.get_client_info() == _client_info()
    assert oauth_path.stat().st_mode & 0o777 == 0o600
    assert oauth_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_storage_keeps_service_files_separate(tmp_path: Path) -> None:
    gmail = FileTokenStorage(tmp_path / 'oauth' / 'gmail.json')
    drive = FileTokenStorage(tmp_path / 'oauth' / 'drive.json')

    await gmail.set_tokens(_tokens())

    assert await gmail.get_tokens() == _tokens()
    assert await drive.get_tokens() is None


@pytest.mark.asyncio
async def test_storage_rejects_an_open_directory(oauth_path: Path) -> None:
    oauth_path.parent.mkdir(mode=0o755)

    with pytest.raises(ValueError, match='OAuth directory mode must be 0700'):
        await FileTokenStorage(oauth_path).set_tokens(_tokens())


@pytest.mark.asyncio
async def test_storage_rejects_an_open_file(oauth_path: Path) -> None:
    oauth_path.parent.mkdir(mode=0o700)
    oauth_path.write_text('{}', encoding='utf-8')
    oauth_path.chmod(0o644)

    with pytest.raises(ValueError, match='OAuth file mode must be 0600'):
        await FileTokenStorage(oauth_path).get_tokens()


@pytest.mark.asyncio
async def test_storage_rejects_a_symlink(oauth_path: Path) -> None:
    oauth_path.parent.mkdir(mode=0o700)
    target = oauth_path.parent / 'target.json'
    target.write_text('{}', encoding='utf-8')
    target.chmod(0o600)
    oauth_path.symlink_to(target)

    with pytest.raises(ValueError, match='OAuth file must be regular'):
        await FileTokenStorage(oauth_path).get_tokens()


@pytest.mark.asyncio
async def test_storage_rejects_a_foreign_schema(oauth_path: Path) -> None:
    oauth_path.parent.mkdir(mode=0o700)
    oauth_path.write_text('{"version": 2}', encoding='utf-8')
    oauth_path.chmod(0o600)

    with pytest.raises(ValueError, match='OAuth state is invalid'):
        await FileTokenStorage(oauth_path).get_tokens()


def test_storage_repr_excludes_path_and_secrets(oauth_path: Path) -> None:
    storage = FileTokenStorage(oauth_path)
    rendered = repr(storage)

    assert str(oauth_path) not in rendered
    assert 'access' not in rendered
    assert 'refresh' not in rendered


@pytest.mark.asyncio
async def test_storage_rejects_a_special_file(oauth_path: Path) -> None:
    oauth_path.parent.mkdir(mode=0o700)
    os.mkfifo(oauth_path, mode=0o600)

    with pytest.raises(ValueError, match='OAuth file must be regular'):
        await FileTokenStorage(oauth_path).get_tokens()
