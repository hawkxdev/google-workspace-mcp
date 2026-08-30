import base64
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from google_workspace_mcp.auth.state import OAuthState
from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.cli.cutover import (
    CutoverError,
    CutoverJournal,
    IdentityManifest,
    MaintenanceAttestation,
    ResetManifest,
    SnapshotManifest,
    _check_proc_net_tcp,
    _load_secure_json,
    _write_atomic_secure_file,
    apply_reset_manifest,
    assert_mutation_exclusion,
    assert_services_quiescent,
    build_identity_manifest,
    build_reset_manifest,
    create_cutover_journal,
    create_maintenance_attestation,
    create_offline_snapshot,
    main,
    mark_gate_opened,
    reconcile_gate,
    restore_offline_snapshot,
    verify_maintenance_attestation,
    verify_offline_snapshot,
)


@pytest.fixture
def current_uid() -> int:
    return os.getuid()


@pytest.fixture
def env_dir(tmp_path: Path, current_uid: int) -> Path:
    envs_path = tmp_path / 'etc_google_mcp'
    envs_path.mkdir(mode=0o700, parents=True)
    os.chmod(envs_path, 0o700)
    ports = {
        'gmail': 8431,
        'calendar': 8432,
        'drive': 8433,
        'sheets': 8434,
        'docs': 8435,
    }
    for svc in SERVICES:
        env_file = envs_path / f'{svc}.env'
        svc_upper = svc.upper()
        content = (
            f'{svc_upper}_MCP_PUBLIC_URL=https://mcp.example.test/{svc}\n'
            f'{svc_upper}_MCP_PATH=/{svc}/mcp\n'
            f'{svc_upper}_MCP_PORT={ports[svc]}\n'
            f'{svc_upper}_OAUTH_STATE_PATH={tmp_path}/state/{svc}/oauth_state.sqlite3\n'
            f'{svc_upper}_GOOGLE_TOKEN_PATH={tmp_path}/state/{svc}/google_token.json\n'
        )
        env_file.write_text(content, encoding='utf-8')
        os.chmod(env_file, 0o600)
    return envs_path


@pytest.fixture
def state_root(tmp_path: Path, current_uid: int) -> Path:
    root = tmp_path / 'state'
    root.mkdir(mode=0o700, parents=True)
    os.chmod(root, 0o700)
    for svc in SERVICES:
        svc_dir = root / svc
        svc_dir.mkdir(mode=0o700, parents=True)
        os.chmod(svc_dir, 0o700)
        db_file = svc_dir / 'oauth_state.sqlite3'
        db_file.write_bytes(b'placeholder')
        os.chmod(db_file, 0o600)
        token_file = svc_dir / 'google_token.json'
        token_file.write_text('{"token": "secret"}', encoding='utf-8')
        os.chmod(token_file, 0o600)
    return root


@pytest.fixture
def identity_manifest(env_dir: Path, current_uid: int) -> IdentityManifest:
    return build_identity_manifest(env_dir, expected_uid=current_uid)


def test_identity_manifest_requires_five_unique_services(
    env_dir: Path, current_uid: int
) -> None:
    manifest = build_identity_manifest(env_dir, expected_uid=current_uid)

    assert tuple(item.service_id for item in manifest.services) == (
        'gmail',
        'calendar',
        'drive',
        'sheets',
        'docs',
    )
    assert len({item.issuer for item in manifest.services}) == 5
    assert len({item.resource for item in manifest.services}) == 5
    assert len({item.state_path for item in manifest.services}) == 5
    assert len({item.google_token_path for item in manifest.services}) == 5
    assert len({item.port for item in manifest.services}) == 5
    assert all(
        item.resource == f'{item.issuer}/mcp' for item in manifest.services
    )
    assert len(manifest.digest) == 64


@pytest.mark.parametrize(
    'corrupt_key,corrupt_val',
    [
        ('GMAIL_MCP_PUBLIC_URL', 'https://mcp.example.test/calendar'),
        ('GMAIL_MCP_PATH', '/calendar/mcp'),
        ('GMAIL_MCP_PORT', '8432'),
        (
            'GMAIL_OAUTH_STATE_PATH',
            '/other/state/gmail/oauth_state.sqlite3_bad',
        ),
        ('GMAIL_GOOGLE_TOKEN_PATH', '/other/state/gmail/token_bad.json'),
    ],
)
def test_identity_manifest_rejects_collisions(
    env_dir: Path, current_uid: int, corrupt_key: str, corrupt_val: str
) -> None:
    env_file = env_dir / 'gmail.env'
    lines = env_file.read_text(encoding='utf-8').splitlines()
    new_lines = []
    for line in lines:
        if line.startswith(corrupt_key.split('=')[0]):
            new_lines.append(f'{corrupt_key}={corrupt_val}')
        else:
            new_lines.append(line)
    env_file.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')

    with pytest.raises(
        CutoverError, match='collision|duplicate|identity|invalid'
    ):
        build_identity_manifest(env_dir, expected_uid=current_uid)


def test_identity_manifest_rejects_cross_service_state_collision(
    env_dir: Path, current_uid: int, tmp_path: Path
) -> None:
    calendar_env = env_dir / 'calendar.env'
    calendar_env.write_text(
        'CALENDAR_MCP_PUBLIC_URL=https://mcp.example.test/calendar\n'
        'CALENDAR_MCP_PATH=/calendar/mcp\n'
        'CALENDAR_MCP_PORT=8432\n'
        f'CALENDAR_OAUTH_STATE_PATH={tmp_path}/state/gmail/oauth_state.sqlite3\n'
        f'CALENDAR_GOOGLE_TOKEN_PATH={tmp_path}/state/calendar/google_token.json\n',
        encoding='utf-8',
    )
    with pytest.raises(CutoverError, match='state path|collision|invalid'):
        build_identity_manifest(env_dir, expected_uid=current_uid)


def test_identity_manifest_rejects_missing_or_extra_keys(
    env_dir: Path, current_uid: int
) -> None:
    env_file = env_dir / 'gmail.env'
    lines = [
        line
        for line in env_file.read_text(encoding='utf-8').splitlines()
        if not line.startswith('GMAIL_GOOGLE_TOKEN_PATH')
    ]
    env_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    with pytest.raises(CutoverError, match='missing required key'):
        build_identity_manifest(env_dir, expected_uid=current_uid)


def test_identity_manifest_rejects_shell_expansion_syntax(
    env_dir: Path, current_uid: int
) -> None:
    env_file = env_dir / 'gmail.env'
    env_file.write_text(
        'GMAIL_MCP_PUBLIC_URL=https://mcp.example.test/$(whoami)\n'
        'GMAIL_MCP_PATH=/gmail/mcp\n'
        'GMAIL_MCP_PORT=8431\n'
        '/tmp/state/gmail/oauth_state.sqlite3\n'
        'GMAIL_GOOGLE_TOKEN_PATH=/tmp/state/gmail/google_token.json\n',
        encoding='utf-8',
    )
    with pytest.raises(CutoverError, match='shell expansion'):
        build_identity_manifest(env_dir, expected_uid=current_uid)


def test_identity_manifest_rejects_insecure_permissions_or_symlinks(
    env_dir: Path, current_uid: int, tmp_path: Path
) -> None:
    real_file = tmp_path / 'real.env'
    real_file.write_text('content', encoding='utf-8')
    sym_file = env_dir / 'gmail.env'
    sym_file.unlink()
    sym_file.symlink_to(real_file)

    with pytest.raises(CutoverError, match='symlink|insecure|provenance'):
        build_identity_manifest(env_dir, expected_uid=current_uid)


def test_identity_manifest_rejects_intersecting_state_and_token_paths(
    env_dir: Path, current_uid: int, tmp_path: Path
) -> None:
    env_file = env_dir / 'gmail.env'
    env_file.write_text(
        'GMAIL_MCP_PUBLIC_URL=https://mcp.example.test/gmail\n'
        'GMAIL_MCP_PATH=/gmail/mcp\n'
        'GMAIL_MCP_PORT=8431\n'
        f'GMAIL_OAUTH_STATE_PATH={tmp_path}/state/gmail/oauth_state.sqlite3\n'
        f'GMAIL_GOOGLE_TOKEN_PATH={tmp_path}/state/calendar/google_token.json\n',
        encoding='utf-8',
    )
    with pytest.raises(
        CutoverError, match='intersecting|collision|token path'
    ):
        build_identity_manifest(env_dir, expected_uid=current_uid)


def test_reset_manifest_contains_only_oauth_state(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    manifest = build_reset_manifest(
        identity_manifest,
        state_root,
        expected_uid=current_uid,
    )
    names = {entry.path.name for entry in manifest.entries}

    assert names <= {
        'oauth_state.sqlite3',
        'oauth_state.sqlite3-wal',
        'oauth_state.sqlite3-shm',
        'oauth_state.sqlite3.init-lock',
        'oauth_state.sqlite3-journal',
    }
    assert 'google_token.json' not in names
    assert len(manifest.directories) == 5
    assert len(manifest.digest) == 64


def test_reset_manifest_rejects_unrecognized_oauth_state_prefix(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    rogue = state_root / 'gmail' / 'oauth_state.sqlite3.bak'
    rogue.write_bytes(b'rogue-backup')
    os.chmod(rogue, 0o600)

    with pytest.raises(CutoverError, match='unrecognized'):
        build_reset_manifest(
            identity_manifest,
            state_root,
            expected_uid=current_uid,
        )


def test_reset_manifest_rejects_symlink_dir_or_file(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / 'external_gmail'
    target_dir.mkdir(mode=0o700)
    gmail_dir = state_root / 'gmail'
    for item in gmail_dir.iterdir():
        item.unlink()
    gmail_dir.rmdir()
    gmail_dir.symlink_to(target_dir)

    with pytest.raises(CutoverError, match='symlink|directory'):
        build_reset_manifest(
            identity_manifest,
            state_root,
            expected_uid=current_uid,
        )


def test_reset_manifest_rejects_hardlinked_state_file(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    gmail_db = state_root / 'gmail' / 'oauth_state.sqlite3'
    hardlink_target = tmp_path / 'hardlink.sqlite3'
    os.link(gmail_db, hardlink_target)

    with pytest.raises(CutoverError, match='hard link'):
        build_reset_manifest(
            identity_manifest,
            state_root,
            expected_uid=current_uid,
        )


def test_reset_manifest_rejects_insecure_mode_on_state_file(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    gmail_db = state_root / 'gmail' / 'oauth_state.sqlite3'
    os.chmod(gmail_db, 0o666)

    with pytest.raises(CutoverError, match='permissions'):
        build_reset_manifest(
            identity_manifest,
            state_root,
            expected_uid=current_uid,
        )


def test_reset_manifest_rejects_wrong_owner_on_state_file(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    with pytest.raises(CutoverError, match='owned by'):
        build_reset_manifest(
            identity_manifest,
            state_root,
            expected_uid=current_uid + 999,
        )


def test_reset_manifest_rejects_service_directory_mismatch(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    wrong_root = tmp_path / 'wrong_root'
    wrong_root.mkdir(mode=0o700)
    with pytest.raises(CutoverError, match='directory|root|mismatch|outside'):
        build_reset_manifest(
            identity_manifest,
            wrong_root,
            expected_uid=current_uid,
        )


def test_quiescence_detects_tcp_listen_ports(
    identity_manifest: IdentityManifest,
    tmp_path: Path,
) -> None:
    header = '  sl  local_address rem_address   st\n'
    row = '   0: 0100007F:20EF 00000000:0000 0A\n'
    proc_tcp = tmp_path / 'tcp'
    proc_tcp.write_text(f'{header}{row}', encoding='utf-8')
    proc_tcp6 = tmp_path / 'tcp6'
    proc_tcp6.write_text(header, encoding='utf-8')

    with pytest.raises(CutoverError, match='LISTEN|port|quiescent'):
        assert_services_quiescent(
            identity_manifest,
            proc_net_tcp_path=proc_tcp,
            proc_net_tcp6_path=proc_tcp6,
            skip_systemd_check=True,
            allow_non_linux=True,
        )


def test_quiescence_detects_tcp6_listen_ports(
    identity_manifest: IdentityManifest,
    tmp_path: Path,
) -> None:
    header = '  sl  local_address rem_address   st\n'
    row6 = '   0: 00000000000000000000000000000001:20F1 000000000000:0000 0A\n'
    proc_tcp = tmp_path / 'tcp'
    proc_tcp.write_text(header, encoding='utf-8')
    proc_tcp6 = tmp_path / 'tcp6'
    proc_tcp6.write_text(f'{header}{row6}', encoding='utf-8')

    with pytest.raises(CutoverError, match='LISTEN|port|quiescent'):
        assert_services_quiescent(
            identity_manifest,
            proc_net_tcp_path=proc_tcp,
            proc_net_tcp6_path=proc_tcp6,
            skip_systemd_check=True,
            allow_non_linux=True,
        )


def test_quiescence_detects_non_host_network_namespace(
    identity_manifest: IdentityManifest,
    tmp_path: Path,
) -> None:
    empty_tcp = tmp_path / 'empty_tcp'
    empty_tcp.write_text(
        '  sl  local_address rem_address   st\n', encoding='utf-8'
    )
    with (
        patch(
            'google_workspace_mcp.cli.cutover._check_network_namespace_is_host',
            side_effect=CutoverError(
                'quiescence check must run in host network namespace'
            ),
        ),
        pytest.raises(CutoverError, match='host network namespace'),
    ):
        assert_services_quiescent(
            identity_manifest,
            proc_net_tcp_path=empty_tcp,
            proc_net_tcp6_path=empty_tcp,
            skip_systemd_check=True,
            allow_non_linux=True,
        )


def test_assert_services_quiescent_systemd_active_check(
    identity_manifest: IdentityManifest,
    tmp_path: Path,
) -> None:
    empty_tcp = tmp_path / 'empty_tcp'
    empty_tcp.write_text(
        '  sl  local_address rem_address   st\n', encoding='utf-8'
    )

    # 1. Active unit raises CutoverError
    active_proc = MagicMock(returncode=0, stdout='active\n')
    with (
        patch('subprocess.run', return_value=active_proc),
        pytest.raises(CutoverError, match='active or activating'),
    ):
        assert_services_quiescent(
            identity_manifest,
            proc_net_tcp_path=empty_tcp,
            proc_net_tcp6_path=empty_tcp,
            skip_systemd_check=False,
            allow_non_linux=True,
        )

    # 2. Inactive units pass
    inactive_proc = MagicMock(returncode=3, stdout='inactive\n')
    with patch('subprocess.run', return_value=inactive_proc):
        assert_services_quiescent(
            identity_manifest,
            proc_net_tcp_path=empty_tcp,
            proc_net_tcp6_path=empty_tcp,
            skip_systemd_check=False,
            allow_non_linux=True,
        )

    # 3. systemctl execution error raises CutoverError
    with (
        patch(
            'subprocess.run',
            side_effect=FileNotFoundError('systemctl not found'),
        ),
        pytest.raises(CutoverError, match='failed to check systemd status'),
    ):
        assert_services_quiescent(
            identity_manifest,
            proc_net_tcp_path=empty_tcp,
            proc_net_tcp6_path=empty_tcp,
            skip_systemd_check=False,
            allow_non_linux=True,
        )


def test_mutation_exclusion_detects_unmasked_template(
    identity_manifest: IdentityManifest,
) -> None:
    with (
        patch(
            'google_workspace_mcp.cli.cutover._is_unit_masked',
            return_value=False,
        ),
        pytest.raises(CutoverError, match='template unit.*masked'),
    ):
        assert_mutation_exclusion(identity_manifest)


def test_mutation_exclusion_detects_unmasked_instance(
    identity_manifest: IdentityManifest,
) -> None:
    def mock_masked(unit_name: str) -> bool:
        return unit_name != 'google-mcp@gmail.service'

    with (
        patch(
            'google_workspace_mcp.cli.cutover._is_unit_masked',
            side_effect=mock_masked,
        ),
        pytest.raises(
            CutoverError, match='unit google-mcp@gmail.service.*masked'
        ),
    ):
        assert_mutation_exclusion(identity_manifest)


def test_assert_mutation_exclusion_checks_companion_activation_units(
    identity_manifest: IdentityManifest,
    tmp_path: Path,
) -> None:
    with (
        patch(
            'google_workspace_mcp.cli.cutover._is_unit_masked',
            return_value=True,
        ),
        patch(
            'google_workspace_mcp.cli.cutover._check_no_activation_units',
            side_effect=CutoverError(
                'activation unit google-mcp@gmail.socket exists'
            ),
        ),
        pytest.raises(CutoverError, match='activation unit.*exists'),
    ):
        assert_mutation_exclusion(identity_manifest)


def test_apply_refuses_changed_manifest_entry(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    target_entry = next(e for e in preview.entries if e.present)
    target_entry.path.write_bytes(b'changed-after-preview')

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(CutoverError, match='manifest entry changed|sha256'),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )


def test_apply_refuses_if_previously_absent_file_appears(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    wal = state_root / 'gmail' / 'oauth_state.sqlite3-wal'
    wal.write_bytes(b'wal-content')
    os.chmod(wal, 0o600)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(CutoverError, match='manifest entry changed|absent'),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )


def test_apply_refuses_when_service_directory_replaced(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    gmail_dir = state_root / 'gmail'
    shutil.rmtree(gmail_dir)
    gmail_dir.mkdir(mode=0o700)
    db_file = gmail_dir / 'oauth_state.sqlite3'
    db_file.write_bytes(b'placeholder')
    os.chmod(db_file, 0o600)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(CutoverError, match='directory.*changed'),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )


def test_apply_refuses_when_identity_digest_changes(
    env_dir: Path,
    state_root: Path,
    current_uid: int,
) -> None:
    id_manifest = build_identity_manifest(env_dir, expected_uid=current_uid)
    preview = build_reset_manifest(
        id_manifest, state_root, expected_uid=current_uid
    )

    gmail_env = env_dir / 'gmail.env'
    content = gmail_env.read_text('utf-8').replace(
        'GMAIL_MCP_PORT=8431', 'GMAIL_MCP_PORT=8439'
    )
    gmail_env.write_text(content, 'utf-8')

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(
            CutoverError, match='live identity digest does not match'
        ),
    ):
        apply_reset_manifest(
            id_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )


def test_apply_reset_manifest_handles_root_env_and_service_state_uid(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    # Rebuild uses expected_env_uid (default 0 or current_uid in tests),
    # even when state directory owner is a service UID.
    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )


def test_apply_zero_deletion_on_preflight_failure(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    docs_db = state_root / 'docs' / 'oauth_state.sqlite3'
    docs_db.write_bytes(b'docs-corrupted')

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(CutoverError),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )

    for svc in SERVICES:
        assert (state_root / svc / 'oauth_state.sqlite3').exists()


def test_apply_succeeds_and_deletes_only_oauth_state(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )

    for svc in SERVICES:
        assert not (state_root / svc / 'oauth_state.sqlite3').exists()
        assert (state_root / svc / 'google_token.json').exists()


def test_maintenance_attestation_create_and_verify(
    identity_manifest: IdentityManifest,
    tmp_path: Path,
) -> None:
    att_path = tmp_path / 'maintenance.attestation'
    attestation = create_maintenance_attestation(
        identity=identity_manifest,
        path=att_path,
        nginx_master_pid=1234,
        nginx_config_digest='a' * 64,
        maintenance_include_target='/etc/nginx/snippets/maintenance.inc',
        worker_generation=3,
    )
    assert att_path.exists()
    assert attestation.nginx_master_pid == 1234
    assert len(attestation.digest) == 64

    verify_maintenance_attestation(
        identity_manifest,
        attestation,
        expected_nginx_master_pid=1234,
        expected_nginx_config_digest='a' * 64,
        expected_include_target='/etc/nginx/snippets/maintenance.inc',
        expected_worker_generation=3,
    )

    corrupt_attestation = MaintenanceAttestation(
        identity_digest=identity_manifest.digest,
        nginx_master_pid=9999,
        nginx_config_digest='a' * 64,
        maintenance_include_target='/etc/nginx/snippets/maintenance.inc',
        worker_generation=3,
        digest='b' * 64,
    )
    with pytest.raises(CutoverError, match='attestation'):
        verify_maintenance_attestation(identity_manifest, corrupt_attestation)


def test_offline_snapshot_create_verify_and_restore_roundtrip(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    gmail_db = state_root / 'gmail' / 'oauth_state.sqlite3'
    gmail_db.unlink()
    with sqlite3.connect(gmail_db) as conn:
        conn.execute('CREATE TABLE test_data (id INT, val TEXT);')
        conn.execute("INSERT INTO test_data VALUES (1, 'persisted_val');")
        conn.commit()
    os.chmod(gmail_db, 0o600)

    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snapshot_backup'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity=identity_manifest,
            reset=reset_manifest,
            destination=dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )
    verify_offline_snapshot(snapshot, expected_uid=current_uid)

    att_path = tmp_path / 'm.att'
    attestation = create_maintenance_attestation(
        identity_manifest,
        att_path,
        nginx_master_pid=100,
        nginx_config_digest='c' * 64,
        maintenance_include_target='/etc/nginx/snippets/m.inc',
        worker_generation=1,
    )

    journal_path = tmp_path / 'cutover.journal'
    journal = create_cutover_journal(
        identity=identity_manifest,
        reset=reset_manifest,
        snapshot=snapshot,
        maintenance=attestation,
        path=journal_path,
    )
    assert journal.state == 'PRE_GATE'

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        apply_reset_manifest(
            identity_manifest,
            reset_manifest,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )
    assert not gmail_db.exists()

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        restore_offline_snapshot(
            identity=identity_manifest,
            snapshot=snapshot,
            journal=journal,
            maintenance=attestation,
            state_root=state_root,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )

    assert gmail_db.exists()
    with sqlite3.connect(gmail_db) as conn:
        row = conn.execute('SELECT val FROM test_data WHERE id=1;').fetchone()
        assert row == ('persisted_val',)


def test_restore_offline_snapshot_preflight_prevents_partial_deletion(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap_preflight_test'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity=identity_manifest,
            reset=reset_manifest,
            destination=dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )

    att_path = tmp_path / 'm.att'
    attestation = create_maintenance_attestation(
        identity_manifest,
        att_path,
        nginx_master_pid=100,
        nginx_config_digest='c' * 64,
        maintenance_include_target='/etc/nginx/snippets/m.inc',
        worker_generation=1,
    )

    journal_path = tmp_path / 'cutover.journal'
    journal = create_cutover_journal(
        identity=identity_manifest,
        reset=reset_manifest,
        snapshot=snapshot,
        maintenance=attestation,
        path=journal_path,
    )

    # Corrupt the 5th service (docs) snapshot source file.
    docs_snap_file = dest / 'docs' / 'oauth_state.sqlite3'
    docs_snap_file.write_bytes(b'corrupted-docs-snapshot')

    # Seed identifiable content in services 1-4 destination state files.
    for svc in ('gmail', 'calendar', 'drive', 'sheets'):
        (state_root / svc / 'oauth_state.sqlite3').write_bytes(
            f'preserved-{svc}'.encode()
        )

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(
            CutoverError, match='sha256 mismatch|inaccessible|size mismatch'
        ),
    ):
        restore_offline_snapshot(
            identity=identity_manifest,
            snapshot=snapshot,
            journal=journal,
            maintenance=attestation,
            state_root=state_root,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )

    # Verify zero-deletion: services 1-4 destination files remain untouched!
    for svc in ('gmail', 'calendar', 'drive', 'sheets'):
        db_path = state_root / svc / 'oauth_state.sqlite3'
        assert db_path.exists()
        assert db_path.read_bytes() == f'preserved-{svc}'.encode()


def test_restore_offline_snapshot_fchowns_restored_files(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap_fchown'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity=identity_manifest,
            reset=reset_manifest,
            destination=dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )

    att_path = tmp_path / 'm.att'
    attestation = create_maintenance_attestation(
        identity_manifest,
        att_path,
        nginx_master_pid=100,
        nginx_config_digest='c' * 64,
        maintenance_include_target='/etc/nginx/snippets/m.inc',
        worker_generation=1,
    )

    journal_path = tmp_path / 'cutover.journal'
    journal = create_cutover_journal(
        identity=identity_manifest,
        reset=reset_manifest,
        snapshot=snapshot,
        maintenance=attestation,
        path=journal_path,
    )

    fchown_calls: list[tuple[int, int, int]] = []

    def mock_fchown(fd: int, uid: int, gid: int) -> None:
        fchown_calls.append((fd, uid, gid))

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover._validate_root_provenance_directory',
            side_effect=lambda p, uid: os.open(p, os.O_RDONLY),
        ),
        patch('os.fchown', side_effect=mock_fchown),
    ):
        restore_offline_snapshot(
            identity=identity_manifest,
            snapshot=snapshot,
            journal=journal,
            maintenance=attestation,
            state_root=state_root,
            expected_uid=1234,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )

    assert len(fchown_calls) >= 5
    assert all(call[1] == 1234 for call in fchown_calls)


def test_restore_permanently_refuses_after_gate_opened(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity_manifest,
            reset_manifest,
            dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )
    att_path = tmp_path / 'm.att'
    attestation = create_maintenance_attestation(
        identity_manifest,
        att_path,
        nginx_master_pid=100,
        nginx_config_digest='c' * 64,
        maintenance_include_target='/etc/nginx/snippets/m.inc',
        worker_generation=1,
    )
    journal_path = tmp_path / 'cutover.journal'
    journal = create_cutover_journal(
        identity_manifest, reset_manifest, snapshot, attestation, journal_path
    )

    opened_journal = mark_gate_opened(journal)
    assert opened_journal.state == 'GATE_OPENED'

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(CutoverError, match='gate has been opened'),
    ):
        restore_offline_snapshot(
            identity=identity_manifest,
            snapshot=snapshot,
            journal=opened_journal,
            maintenance=attestation,
            state_root=state_root,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )


def test_reconcile_gate_behavior(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap2'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity_manifest,
            reset_manifest,
            dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )
    att_path = tmp_path / 'm.att'
    attestation = create_maintenance_attestation(
        identity_manifest,
        att_path,
        nginx_master_pid=100,
        nginx_config_digest='c' * 64,
        maintenance_include_target='/etc/nginx/snippets/m.inc',
        worker_generation=1,
    )
    journal_path = tmp_path / 'cutover.journal'
    journal = create_cutover_journal(
        identity_manifest, reset_manifest, snapshot, attestation, journal_path
    )

    outcome = reconcile_gate(journal)
    assert outcome == 'enforce_maintenance'

    opened_journal = mark_gate_opened(journal)
    outcome_opened = reconcile_gate(opened_journal)
    assert outcome_opened == 'enforce_active_roll_forward'


def test_reconcile_gate_crash_boundary_states(tmp_path: Path) -> None:
    corrupt_journal = CutoverJournal(
        path=tmp_path / 'j.json',
        state='CORRUPTED_STATE',
        identity_digest='a' * 64,
        reset_digest='b' * 64,
        snapshot_digest='c' * 64,
        maintenance_digest='d' * 64,
        digest='e' * 64,
    )
    with pytest.raises(CutoverError, match='unknown journal state'):
        reconcile_gate(corrupt_journal)


def test_cutover_journal_cross_digest_assertions_and_pre_gate_guard(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap_cross'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity_manifest,
            reset_manifest,
            dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )
    att_path = tmp_path / 'm.att'
    attestation = create_maintenance_attestation(
        identity_manifest,
        att_path,
        nginx_master_pid=100,
        nginx_config_digest='c' * 64,
        maintenance_include_target='/etc/nginx/snippets/m.inc',
        worker_generation=1,
    )

    # 1. Mismatched reset identity digest
    corrupt_reset = ResetManifest(
        state_root=reset_manifest.state_root,
        identity_digest='x' * 64,
        directories=reset_manifest.directories,
        entries=reset_manifest.entries,
        digest=reset_manifest.digest,
    )
    with pytest.raises(CutoverError, match='reset manifest identity digest'):
        create_cutover_journal(
            identity_manifest,
            corrupt_reset,
            snapshot,
            attestation,
            tmp_path / 'j.json',
        )

    # 2. Mismatched snapshot identity digest
    corrupt_snap_id = SnapshotManifest(
        destination=snapshot.destination,
        identity_digest='x' * 64,
        reset_digest=snapshot.reset_digest,
        entries=snapshot.entries,
        digest=snapshot.digest,
    )
    with pytest.raises(
        CutoverError, match='snapshot manifest identity digest'
    ):
        create_cutover_journal(
            identity_manifest,
            reset_manifest,
            corrupt_snap_id,
            attestation,
            tmp_path / 'j.json',
        )

    # 3. Mismatched snapshot reset digest
    corrupt_snap_res = SnapshotManifest(
        destination=snapshot.destination,
        identity_digest=snapshot.identity_digest,
        reset_digest='x' * 64,
        entries=snapshot.entries,
        digest=snapshot.digest,
    )
    with pytest.raises(CutoverError, match='snapshot manifest reset digest'):
        create_cutover_journal(
            identity_manifest,
            reset_manifest,
            corrupt_snap_res,
            attestation,
            tmp_path / 'j.json',
        )

    # 4. Mismatched maintenance identity digest
    corrupt_att = MaintenanceAttestation(
        identity_digest='x' * 64,
        nginx_master_pid=attestation.nginx_master_pid,
        nginx_config_digest=attestation.nginx_config_digest,
        maintenance_include_target=attestation.maintenance_include_target,
        worker_generation=attestation.worker_generation,
        digest=attestation.digest,
    )
    with pytest.raises(CutoverError, match='maintenance attestation identity'):
        create_cutover_journal(
            identity_manifest,
            reset_manifest,
            snapshot,
            corrupt_att,
            tmp_path / 'j.json',
        )

    # 5. mark_gate_opened refuses transition from non-PRE_GATE
    journal = create_cutover_journal(
        identity_manifest,
        reset_manifest,
        snapshot,
        attestation,
        tmp_path / 'j.json',
    )
    opened = mark_gate_opened(journal)
    with pytest.raises(CutoverError, match='expected PRE_GATE'):
        mark_gate_opened(opened)


def test_snapshot_and_restore_rebuild_live_identity_manifest(
    env_dir: Path,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    id_manifest = build_identity_manifest(env_dir, expected_uid=current_uid)
    reset_manifest = build_reset_manifest(
        id_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap_live'
    dest.mkdir(mode=0o700)

    # Modify live env file before snapshot create
    gmail_env = env_dir / 'gmail.env'
    content = gmail_env.read_text('utf-8').replace(
        'GMAIL_MCP_PORT=8431', 'GMAIL_MCP_PORT=8439'
    )
    gmail_env.write_text(content, 'utf-8')

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        pytest.raises(
            CutoverError, match='live identity digest does not match'
        ),
    ):
        create_offline_snapshot(
            id_manifest,
            reset_manifest,
            dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )


def test_platform_enforcement_and_proc_net_tcp_fail_closed(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap_plat'
    dest.mkdir(mode=0o700)

    # 1. snapshot create platform enforcement
    with (
        patch('sys.platform', 'darwin'),
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        pytest.raises(CutoverError, match='snapshot create requires Linux'),
    ):
        create_offline_snapshot(
            identity_manifest,
            reset_manifest,
            dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=False,
        )

    # 2. snapshot restore platform enforcement
    journal = CutoverJournal(
        path=tmp_path / 'j.json',
        state='PRE_GATE',
        identity_digest=identity_manifest.digest,
        reset_digest=reset_manifest.digest,
        snapshot_digest='a' * 64,
        maintenance_digest='b' * 64,
        digest='c' * 64,
    )
    att = MaintenanceAttestation(
        identity_digest=identity_manifest.digest,
        nginx_master_pid=1,
        nginx_config_digest='x',
        maintenance_include_target='y',
        worker_generation=1,
        digest='b' * 64,
    )
    snap = SnapshotManifest(
        destination=dest,
        identity_digest=identity_manifest.digest,
        reset_digest=reset_manifest.digest,
        entries=(),
        digest='a' * 64,
    )
    with (
        patch('sys.platform', 'darwin'),
        pytest.raises(CutoverError, match='snapshot restore requires Linux'),
    ):
        restore_offline_snapshot(
            identity_manifest,
            snap,
            journal,
            att,
            state_root,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=False,
        )

    # 3. _check_proc_net_tcp fails closed on Linux when proc table is missing
    missing_proc = tmp_path / 'nonexistent_proc_tcp'
    with (
        patch('sys.platform', 'linux'),
        pytest.raises(
            CutoverError, match='required proc table.*does not exist'
        ),
    ):
        _check_proc_net_tcp(
            missing_proc, forbidden_ports={8431}, allow_non_linux=False
        )


def test_atomic_secure_file_writing(tmp_path: Path) -> None:
    target_file = tmp_path / 'test_atomic.json'
    _write_atomic_secure_file(target_file, b'{"status": "ok"}\n')
    assert target_file.exists()
    assert target_file.read_text(encoding='utf-8') == '{"status": "ok"}\n'
    st = os.stat(target_file)
    assert (st.st_mode & 0o077) == 0

    # Symlinked parent dir rejected
    sym_dir = tmp_path / 'sym_dir'
    sym_dir.symlink_to(tmp_path)
    with pytest.raises(CutoverError, match='parent directory.*symlink'):
        _write_atomic_secure_file(sym_dir / 'fail.json', b'fail')


def test_verify_offline_snapshot_descriptor_relative_security(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    reset_manifest = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    dest = tmp_path / 'snap_verify_test'
    dest.mkdir(mode=0o700)

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        snapshot = create_offline_snapshot(
            identity=identity_manifest,
            reset=reset_manifest,
            destination=dest,
            expected_uid=current_uid,
            expected_env_uid=current_uid,
            allow_non_linux=True,
        )

    # Normal verification passes
    verify_offline_snapshot(snapshot, expected_uid=current_uid)

    # 1. Missing marker fails
    marker = dest / '.complete'
    marker.unlink()
    with pytest.raises(CutoverError, match='completion marker'):
        verify_offline_snapshot(snapshot, expected_uid=current_uid)

    # Restore marker with invalid content
    marker.write_text('BAD', encoding='utf-8')
    os.chmod(marker, 0o600)
    with pytest.raises(
        CutoverError, match='completion marker content invalid'
    ):
        verify_offline_snapshot(snapshot, expected_uid=current_uid)
    marker.write_text('OK\n', encoding='utf-8')
    os.chmod(marker, 0o600)

    # 2. Corrupted file content fails sha256
    gmail_snap = dest / 'gmail' / 'oauth_state.sqlite3'
    gmail_snap.write_bytes(b'corrupted')
    with pytest.raises(CutoverError, match='sha256 mismatch|size mismatch'):
        verify_offline_snapshot(snapshot, expected_uid=current_uid)


def test_toctou_fstat_mismatch_during_read_and_hash(
    env_dir: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    # Test TOCTOU mismatch detection in _load_secure_json
    test_json = tmp_path / 'secure.json'
    test_json.write_text('{"key": "val"}', encoding='utf-8')
    os.chmod(test_json, 0o600)

    orig_fstat = os.fstat
    call_count = 0

    def mock_fstat_changing(fd: int) -> os.stat_result:
        nonlocal call_count
        res = orig_fstat(fd)
        call_count += 1
        if call_count > 1:
            # Simulate inode or size mutation between before and after read
            return os.stat_result(
                (
                    res.st_mode,
                    res.st_ino + 1,
                    res.st_dev,
                    res.st_nlink,
                    res.st_uid,
                    res.st_gid,
                    res.st_size + 10,
                    res.st_atime,
                    res.st_mtime,
                    res.st_ctime,
                )
            )
        return res

    with (
        patch('os.fstat', side_effect=mock_fstat_changing),
        pytest.raises(CutoverError, match='modified during read'),
    ):
        _load_secure_json(test_json, expected_uid=current_uid)


def test_mid_flight_quiescence_or_masking_violation_before_phase2(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
) -> None:
    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )

    check_count = 0

    def mock_quiescent_race(
        identity: IdentityManifest, **kwargs: object
    ) -> None:
        nonlocal check_count
        check_count += 1
        if check_count > 1:
            raise CutoverError(
                'port 8431 is active in LISTEN state mid-flight'
            )

    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            side_effect=mock_quiescent_race,
        ),
        pytest.raises(CutoverError, match='port 8431 is active in LISTEN'),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )

    # Verify that destination files are NOT deleted on mid-flight failure!
    for svc in SERVICES:
        assert (state_root / svc / 'oauth_state.sqlite3').exists()


def test_clean_state_integration_regression(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    gmail_db = state_root / 'gmail' / 'oauth_state.sqlite3'
    gmail_db.unlink()
    download_dir = tmp_path / 'downloads'
    download_dir.mkdir(mode=0o700, exist_ok=True)
    verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()
        )
        .rstrip(b'=')
        .decode('ascii')
    )
    with OAuthState(
        gmail_db,
        service_id='gmail',
        resource='https://mcp.example.test/gmail',
        download_path=download_dir,
    ) as state:
        reg = state.register_client(('https://client.example.test/cb',))
        code = state.issue_authorization_code(
            client_id=reg.client.client_id,
            redirect_uri='https://client.example.test/cb',
            code_challenge=challenge,
            resource='https://mcp.example.test/gmail',
        )
        token = state.redeem_authorization_code(
            code=code,
            client_id=reg.client.client_id,
            client_secret=reg.client_secret,
            redirect_uri='https://client.example.test/cb',
            code_verifier=verifier,
            resource='https://mcp.example.test/gmail',
        )
        assert token.access_token is not None
    os.chmod(gmail_db, 0o600)

    preview = build_reset_manifest(
        identity_manifest, state_root, expected_uid=current_uid
    )
    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        apply_reset_manifest(
            identity_manifest,
            preview,
            allow_non_linux=True,
            expected_env_uid=current_uid,
        )

    new_resource = 'https://mcp.example.test/gmail/mcp'
    with (
        OAuthState(
            gmail_db,
            service_id='gmail',
            resource=new_resource,
            download_path=download_dir,
        ) as state,
        sqlite3.connect(gmail_db) as conn,
    ):
        owners = conn.execute(
            'SELECT service_id, resource FROM state_owner;'
        ).fetchall()
        assert owners == [('gmail', new_resource)]
        client_count = conn.execute(
            'SELECT count(*) FROM clients;'
        ).fetchone()[0]
        assert client_count == 0
        code_count = conn.execute(
            'SELECT count(*) FROM authorization_codes;'
        ).fetchone()[0]
        assert code_count == 0
        token_count = conn.execute(
            'SELECT count(*) FROM access_tokens;'
        ).fetchone()[0]
        assert token_count == 0
        refresh_count = conn.execute(
            'SELECT count(*) FROM refresh_tokens;'
        ).fetchone()[0]
        assert refresh_count == 0


def test_cli_subcommands_json_output_and_secret_free(
    env_dir: Path,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. identity preview
    exit_code = main(
        [
            'identity',
            'preview',
            '--env-dir',
            str(env_dir),
            '--uid',
            str(current_uid),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    identity_data = json.loads(captured.out)
    assert 'digest' in identity_data
    assert len(identity_data['services']) == 5
    assert 'password' not in captured.out.lower()
    assert 'secret' not in captured.out.lower()

    identity_manifest_file = tmp_path / 'identity.json'
    _write_atomic_secure_file(
        identity_manifest_file, captured.out.encode('utf-8')
    )

    # 2. reset preview
    exit_code = main(
        [
            'reset',
            'preview',
            '--env-dir',
            str(env_dir),
            '--state-root',
            str(state_root),
            '--uid',
            str(current_uid),
            '--env-uid',
            str(current_uid),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    reset_data = json.loads(captured.out)
    assert 'digest' in reset_data
    assert len(reset_data['entries']) >= 5

    reset_manifest_file = tmp_path / 'reset.json'
    _write_atomic_secure_file(
        reset_manifest_file, captured.out.encode('utf-8')
    )

    # 3. maintenance attest
    attestation_file = tmp_path / 'm.att'
    exit_code = main(
        [
            'maintenance',
            'attest',
            '--identity-manifest',
            str(identity_manifest_file),
            '--output',
            str(attestation_file),
            '--nginx-master-pid',
            '1234',
            '--nginx-config-digest',
            'a' * 64,
            '--maintenance-include-target',
            '/etc/nginx/snippets/m.inc',
            '--worker-generation',
            '1',
            '--env-uid',
            str(current_uid),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    att_data = json.loads(captured.out)
    assert 'digest' in att_data

    # 4. snapshot create
    snap_dest = tmp_path / 'snap_dir'
    snap_dest.mkdir(mode=0o700)
    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        exit_code = main(
            [
                'snapshot',
                'create',
                '--identity-manifest',
                str(identity_manifest_file),
                '--reset-manifest',
                str(reset_manifest_file),
                '--maintenance-attestation',
                str(attestation_file),
                '--destination',
                str(snap_dest),
                '--uid',
                str(current_uid),
                '--env-uid',
                str(current_uid),
                '--allow-non-linux',
            ]
        )
    assert exit_code == 0
    captured = capsys.readouterr()
    snap_data = json.loads(captured.out)
    assert 'digest' in snap_data

    snap_manifest_file = snap_dest / 'snapshot_manifest.json'

    # 5. snapshot verify
    exit_code = main(
        [
            'snapshot',
            'verify',
            '--manifest',
            str(snap_manifest_file),
            '--uid',
            str(current_uid),
        ]
    )
    assert exit_code == 0
    _ = capsys.readouterr()

    # 6. journal create
    journal_file = tmp_path / 'cutover.journal'
    exit_code = main(
        [
            'journal',
            'create',
            '--identity-manifest',
            str(identity_manifest_file),
            '--reset-manifest',
            str(reset_manifest_file),
            '--snapshot-manifest',
            str(snap_manifest_file),
            '--maintenance-attestation',
            str(attestation_file),
            '--output',
            str(journal_file),
            '--uid',
            str(current_uid),
            '--env-uid',
            str(current_uid),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    journal_data = json.loads(captured.out)
    assert journal_data['state'] == 'PRE_GATE'

    # 7. reset apply
    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        exit_code = main(
            [
                'reset',
                'apply',
                '--identity-manifest',
                str(identity_manifest_file),
                '--manifest',
                str(reset_manifest_file),
                '--confirm-sha256',
                reset_data['digest'],
                '--allow-non-linux',
                '--uid',
                str(current_uid),
                '--env-uid',
                str(current_uid),
            ]
        )
    assert exit_code == 0
    _ = capsys.readouterr()

    # 8. snapshot restore
    with (
        patch(
            'google_workspace_mcp.cli.cutover.assert_services_quiescent',
            return_value=None,
        ),
        patch(
            'google_workspace_mcp.cli.cutover.assert_mutation_exclusion',
            return_value=None,
        ),
    ):
        exit_code = main(
            [
                'snapshot',
                'restore',
                '--identity-manifest',
                str(identity_manifest_file),
                '--manifest',
                str(snap_manifest_file),
                '--journal',
                str(journal_file),
                '--maintenance-attestation',
                str(attestation_file),
                '--state-root',
                str(state_root),
                '--confirm-sha256',
                snap_data['digest'],
                '--allow-non-linux',
                '--uid',
                str(current_uid),
                '--env-uid',
                str(current_uid),
            ]
        )
    assert exit_code == 0
    _ = capsys.readouterr()

    # 9. journal mark-gate-opened
    exit_code = main(
        [
            'journal',
            'mark-gate-opened',
            '--journal',
            str(journal_file),
            '--confirm-sha256',
            journal_data['digest'],
            '--uid',
            str(current_uid),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    opened_data = json.loads(captured.out)
    assert opened_data['state'] == 'GATE_OPENED'


def test_cli_rejects_symlinked_manifest_and_journal_inputs(
    identity_manifest: IdentityManifest,
    state_root: Path,
    current_uid: int,
    tmp_path: Path,
) -> None:
    real_manifest = tmp_path / 'real_manifest.json'
    _write_atomic_secure_file(real_manifest, b'{"status": "ok"}')

    symlinked_manifest = tmp_path / 'sym_manifest.json'
    symlinked_manifest.symlink_to(real_manifest)

    # 1. Rejection in _load_secure_json directly
    with pytest.raises(CutoverError, match='cannot be symlink'):
        _load_secure_json(symlinked_manifest, expected_uid=current_uid)

    # 2. Rejection in CLI subcommand
    exit_code = main(
        [
            'snapshot',
            'verify',
            '--manifest',
            str(symlinked_manifest),
            '--uid',
            str(current_uid),
        ]
    )
    assert exit_code == 1


def test_cutover_entrypoint_exposes_callables() -> None:
    from google_workspace_mcp.cli import cutover

    assert callable(cutover.main)
    assert callable(cutover._entrypoint)
    with patch('sys.argv', ['google-mcp-cutover', '--help']):
        with pytest.raises(SystemExit) as exc:
            cutover._entrypoint()
        assert exc.value.code == 0
