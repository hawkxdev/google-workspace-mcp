"""Владелец файла состояния и запрет писать его в каталог загрузок."""

from pathlib import Path

import pytest

from google_workspace_mcp.auth.state import OAuthState, UnsafeStatePath

GMAIL_RESOURCE = 'https://mcp.example.dev/gmail/mcp'
DRIVE_RESOURCE = 'https://mcp.example.dev/drive/mcp'


def _open(
    path: Path,
    downloads: Path,
    *,
    service_id: str = 'gmail',
    resource: str = GMAIL_RESOURCE,
) -> OAuthState:
    """Открыть состояние сервиса с явным владельцем."""
    return OAuthState(
        path,
        service_id=service_id,
        resource=resource,
        download_path=downloads,
    )


def test_state_created_by_one_service_is_refused_to_another(
    tmp_path: Path,
) -> None:
    """Перепутанный путь — валидная база чужого сервиса."""
    state_path = tmp_path / 'state' / 'oauth.sqlite3'
    downloads = tmp_path / 'downloads'
    downloads.mkdir()

    with _open(state_path, downloads) as state:
        state.register_client(['https://client.example/callback'])

    # Различается ТОЛЬКО сервис: одинаковый resource оставляет решение
    # за проверкой service_id, иначе сработала бы сверка ресурса.
    with pytest.raises(UnsafeStatePath) as excinfo:
        _open(state_path, downloads, service_id='drive')

    assert 'belongs to service' in str(excinfo.value)


def test_state_is_refused_when_resource_differs(tmp_path: Path) -> None:
    """Тот же сервис, другой канонический resource — тоже отказ."""
    state_path = tmp_path / 'state' / 'oauth.sqlite3'
    downloads = tmp_path / 'downloads'
    downloads.mkdir()

    with _open(state_path, downloads) as state:
        state.register_client(['https://client.example/callback'])

    with pytest.raises(UnsafeStatePath):
        _open(
            state_path,
            downloads,
            resource='https://mcp.example.dev/gmail/other',
        )


def test_state_inside_download_directory_is_refused(tmp_path: Path) -> None:
    """Состояние в каталоге, куда пишут инструменты, запрещено."""
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    state_path = downloads / 'nested' / 'oauth.sqlite3'

    with pytest.raises(UnsafeStatePath):
        _open(state_path, downloads)


def test_state_beside_download_directory_is_allowed(tmp_path: Path) -> None:
    """Соседний каталог с общим префиксом имени запретом не задет."""
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    state_path = tmp_path / 'downloads-state' / 'oauth.sqlite3'

    with _open(state_path, downloads) as state:
        assert state.path == state_path.resolve()


def test_legacy_source_inside_download_directory_is_refused(
    tmp_path: Path,
) -> None:
    """Источник миграции тоже не читается из каталога загрузок."""
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    legacy = downloads / 'oauth_clients.json'
    legacy.write_text('{}')
    # Права делаются безопасными намеренно: иначе отказ придёт из
    # проверки прав файла, и мутация guard размещения останется незамечённой.
    downloads.chmod(0o700)
    legacy.chmod(0o600)
    state_path = tmp_path / 'state' / 'oauth.sqlite3'

    state = OAuthState(
        state_path,
        service_id='gmail',
        resource=GMAIL_RESOURCE,
        download_path=downloads,
        legacy_path=legacy,
    )
    try:
        with pytest.raises(UnsafeStatePath):
            state.migrate_legacy()
    finally:
        state.close()
