"""State ownership tests."""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    """Open owned service state."""
    return OAuthState(
        path,
        service_id=service_id,
        resource=resource,
        download_path=downloads,
    )


def test_state_created_by_one_service_is_refused_to_another(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / 'state' / 'oauth.sqlite3'
    downloads = tmp_path / 'downloads'
    downloads.mkdir()

    with _open(state_path, downloads) as state:
        state.register_client(['https://client.example/callback'])

    with pytest.raises(UnsafeStatePath) as excinfo:
        _open(state_path, downloads, service_id='drive')

    assert str(excinfo.value) == 'OAuth state owner mismatch'


def test_state_is_refused_when_resource_differs(tmp_path: Path) -> None:
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


@pytest.mark.parametrize(
    'identities',
    [
        (('gmail', GMAIL_RESOURCE), ('drive', GMAIL_RESOURCE)),
        (('gmail', GMAIL_RESOURCE), ('gmail', DRIVE_RESOURCE)),
    ],
)
def test_competing_owner_initialization_selects_one_identity(
    tmp_path: Path, identities: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    state_path = tmp_path / 'state' / 'oauth.sqlite3'
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    barrier = threading.Barrier(2)

    def open_identity(identity: tuple[str, str]) -> str:
        """Race one owner identity."""
        barrier.wait()
        try:
            with _open(
                state_path,
                downloads,
                service_id=identity[0],
                resource=identity[1],
            ):
                return 'accepted'
        except UnsafeStatePath:
            return 'rejected'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(open_identity, identities))

    assert sorted(outcomes) == ['accepted', 'rejected']
    with sqlite3.connect(state_path) as connection:
        rows = connection.execute(
            'SELECT service_id, resource FROM state_owner'
        ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) in identities


def test_same_identity_waits_for_schema_owner_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / 'state' / 'oauth.sqlite3'
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    entered_schema = threading.Event()
    release_schema = threading.Event()
    original_initialize = OAuthState._initialize_schema

    def delayed_initialize(
        state: OAuthState, *, initialize_owner: bool
    ) -> None:
        """Hold initial owner transaction."""
        if initialize_owner:
            entered_schema.set()
            assert release_schema.wait(timeout=1)
        original_initialize(state, initialize_owner=initialize_owner)

    def open_once() -> None:
        """Open one matching identity."""
        with _open(state_path, downloads):
            pass

    monkeypatch.setattr(OAuthState, '_initialize_schema', delayed_initialize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(open_once)
        assert entered_schema.wait(timeout=1)
        second = pool.submit(open_once)
        time.sleep(0.05)
        assert not second.done()
        release_schema.set()
        first.result(timeout=1)
        second.result(timeout=1)


def test_state_inside_download_directory_is_refused(tmp_path: Path) -> None:
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    state_path = downloads / 'nested' / 'oauth.sqlite3'

    with pytest.raises(UnsafeStatePath):
        _open(state_path, downloads)


def test_state_beside_download_directory_is_allowed(tmp_path: Path) -> None:
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    state_path = tmp_path / 'downloads-state' / 'oauth.sqlite3'

    with _open(state_path, downloads) as state:
        assert state.path == state_path.resolve()


def test_legacy_source_inside_download_directory_is_refused(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    legacy = downloads / 'oauth_clients.json'
    legacy.write_text('{}')
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
