"""Tests for audit logging and service configuration."""

import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from google_workspace_mcp.audit.logger import (
    AuditError,
    AuditLogger,
    validate_audit_path,
)
from google_workspace_mcp.common.config import ServiceConfig


def test_service_config_and_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    safe = tmp_path / 'safe.jsonl'
    for k, v in {
        'GMAIL_AUDIT_LOG_PATH': str(safe),
        'GMAIL_MCP_PUBLIC_URL': 'https://mcp.example.test/gmail/mcp',
        'GMAIL_OAUTH_LOGIN_USERNAME': 'admin',
        'GMAIL_OAUTH_LOGIN_PASSWORD': 'super-secret',
    }.items():
        monkeypatch.setenv(k, v)
    cfg = ServiceConfig.from_env('gmail')
    assert cfg.audit_log_path == safe and cfg.oauth_login_username == 'admin'
    assert cfg.oauth_login_password == 'super-secret'
    assert 'super-secret' not in repr(cfg)
    monkeypatch.delenv('GMAIL_OAUTH_LOGIN_USERNAME', raising=False)
    with pytest.raises(ValueError, match='OAUTH_LOGIN_USERNAME'):
        ServiceConfig.from_env('gmail')
    monkeypatch.setenv('GMAIL_OAUTH_LOGIN_USERNAME', 'admin')
    monkeypatch.delenv('GMAIL_OAUTH_LOGIN_PASSWORD', raising=False)
    with pytest.raises(ValueError, match='OAUTH_LOGIN_PASSWORD'):
        ServiceConfig.from_env('gmail')
    audit, dl = tmp_path / 'a.jsonl', tmp_path / 'dl'
    dl.mkdir(mode=0o700)
    validate_audit_path(audit, dl)
    AuditLogger(audit).log_event({'action': 'auth', 'status': 'ok'})
    assert audit.exists() and oct(audit.stat().st_mode & 0o777) == '0o600'
    with pytest.raises(ValueError, match='download_path collision'):
        validate_audit_path(dl / 'a.jsonl', dl)
    bad = tmp_path / 'unwritable' / 'a.jsonl'
    bad.parent.mkdir(parents=True, mode=0o500)
    with pytest.raises(AuditError, match='Failed to record audit event'):
        AuditLogger(bad).log_event({'action': 'auth'})


def test_validate_audit_path_side_effect_free(tmp_path: Path) -> None:
    target = tmp_path / 'nonexistent' / 'audit.jsonl'
    dl = tmp_path / 'downloads'
    dl.mkdir(mode=0o700)
    validate_audit_path(target, dl)
    assert not target.exists()
    assert not target.parent.exists()


def test_validate_audit_path_rejects_symlink_and_fifo(
    tmp_path: Path,
) -> None:
    dl = tmp_path / 'downloads'
    dl.mkdir(mode=0o700)
    real_file = tmp_path / 'real.jsonl'
    real_file.write_text('{}', encoding='utf-8')
    symlink_file = tmp_path / 'symlink.jsonl'
    symlink_file.symlink_to(real_file)
    with pytest.raises(ValueError, match='invalid audit file target'):
        validate_audit_path(symlink_file, dl)
    fifo_path = tmp_path / 'test.fifo'
    try:
        os.mkfifo(fifo_path)
    except OSError:
        pytest.skip('mkfifo not supported')
    with pytest.raises(ValueError, match='invalid audit file target'):
        validate_audit_path(fifo_path, dl)


def test_validate_audit_path_rejects_directory(tmp_path: Path) -> None:
    dl = tmp_path / 'downloads'
    dl.mkdir(mode=0o700)
    audit_dir = tmp_path / 'audit_dir'
    audit_dir.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='invalid audit file target'):
        validate_audit_path(audit_dir, dl)


def test_audit_logger_secure_creation_and_append(tmp_path: Path) -> None:
    audit_file = tmp_path / 'nested' / 'dir' / 'audit.jsonl'
    logger = AuditLogger(audit_file)
    logger.log_event({'op': 'login', 'user': 'alice'})
    logger.log_event({'op': 'rotate', 'status': 'ok'})
    parent_mode = oct(audit_file.parent.stat().st_mode & 0o777)
    file_mode = oct(audit_file.stat().st_mode & 0o777)
    assert parent_mode == '0o700'
    assert file_mode == '0o600'
    lines = audit_file.read_text(encoding='utf-8').strip().split('\n')
    assert len(lines) == 2
    assert json.loads(lines[0]) == {'op': 'login', 'user': 'alice'}
    assert json.loads(lines[1]) == {'op': 'rotate', 'status': 'ok'}


def test_audit_logger_rejects_symlink_parent(tmp_path: Path) -> None:
    real_dir = tmp_path / 'real_dir'
    real_dir.mkdir(mode=0o700)
    link_dir = tmp_path / 'link_dir'
    link_dir.symlink_to(real_dir)
    target = link_dir / 'audit.jsonl'
    logger = AuditLogger(target)
    with pytest.raises(AuditError, match='Insecure directory target'):
        logger.log_event({'op': 'test'})


def test_audit_logger_concurrent_append(tmp_path: Path) -> None:
    audit_file = tmp_path / 'concurrent' / 'audit.jsonl'
    logger = AuditLogger(audit_file)
    num_events = 50

    def _write_event(idx: int) -> None:
        logger.log_event({'index': idx, 'status': 'ok'})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_write_event, i) for i in range(num_events)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    lines = audit_file.read_text(encoding='utf-8').strip().split('\n')
    assert len(lines) == num_events
    indices = {json.loads(line)['index'] for line in lines}
    assert indices == set(range(num_events))


def test_audit_logger_no_secret_or_raw_path_in_error(
    tmp_path: Path,
) -> None:
    unwritable = tmp_path / 'unwritable_dir' / 'audit.jsonl'
    unwritable.parent.mkdir(parents=True, mode=0o500)
    logger = AuditLogger(unwritable)
    try:
        logger.log_event({'secret_data': 'super-secret-token'})
    except AuditError as exc:
        msg = str(exc)
        assert str(unwritable) not in msg
        assert 'super-secret-token' not in msg
        assert msg == 'Failed to record audit event'
    else:
        pytest.fail('AuditError was not raised')


def _worker_log_events(audit_path: Path, event_ids: list[int]) -> None:
    """Log worker audit events."""
    logger = AuditLogger(audit_path)
    for event_id in event_ids:
        logger.log_event({'event_id': event_id, 'worker': os.getpid()})


def test_audit_logger_cross_process_append(tmp_path: Path) -> None:
    audit_file = tmp_path / 'mp_audit' / 'audit.jsonl'
    ctx = multiprocessing.get_context('fork')
    num_workers = 4
    events_per_worker = 15
    processes: list[multiprocessing.Process] = []
    expected_ids: set[int] = set()

    for worker in range(num_workers):
        start_id = worker * events_per_worker
        worker_ids = list(range(start_id, start_id + events_per_worker))
        expected_ids.update(worker_ids)
        process = ctx.Process(
            target=_worker_log_events,
            args=(audit_file, worker_ids),
        )
        processes.append(process)
        process.start()

    for process in processes:
        process.join(timeout=10.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            pytest.fail('Audit worker did not exit')
        assert process.exitcode == 0

    lines = audit_file.read_text(encoding='utf-8').splitlines()
    total_events = num_workers * events_per_worker
    assert len(lines) == total_events
    logged_ids = {json.loads(line)['event_id'] for line in lines}
    assert logged_ids == expected_ids
