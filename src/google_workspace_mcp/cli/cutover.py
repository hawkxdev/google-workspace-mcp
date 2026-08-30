"""Manage service cutover safety."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.common.config import _validate_service_identity

# Authoritative OAuth state artifacts.
AUTHORITATIVE_STATE_NAMES: frozenset[str] = frozenset(
    {
        'oauth_state.sqlite3',
        'oauth_state.sqlite3-wal',
        'oauth_state.sqlite3-shm',
        'oauth_state.sqlite3.init-lock',
        'oauth_state.sqlite3-journal',
    }
)

_DIRECTORY_FLAGS: int = (
    os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
)
_NOFOLLOW: int = getattr(os, 'O_NOFOLLOW', 0)


class CutoverError(Exception):
    """Report cutover safety failure."""


# Data models


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """Store directory identity record."""

    service_id: str
    path: Path
    device: int
    inode: int
    owner: int
    mode: int


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    """Store service identity record."""

    service_id: str
    issuer: str
    resource: str
    mcp_path: str
    port: int
    state_path: Path
    google_token_path: Path


@dataclass(frozen=True, slots=True)
class IdentityManifest:
    """Store identity manifest record."""

    env_dir: Path
    services: tuple[ServiceIdentity, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class ResetEntry:
    """Store reset entry metadata."""

    path: Path
    present: bool
    device: int | None
    inode: int | None
    owner: int | None
    mode: int | None
    links: int | None
    size: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class ResetManifest:
    """Store reset manifest data."""

    state_root: Path
    identity_digest: str
    directories: tuple[DirectoryIdentity, ...]
    entries: tuple[ResetEntry, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """Store snapshot entry metadata."""

    service_id: str
    name: str
    path: Path
    present: bool
    size: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Store snapshot manifest data."""

    destination: Path
    identity_digest: str
    reset_digest: str
    entries: tuple[SnapshotEntry, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class MaintenanceAttestation:
    """Store maintenance attestation record."""

    identity_digest: str
    nginx_master_pid: int
    nginx_config_digest: str
    maintenance_include_target: str
    worker_generation: int
    digest: str


@dataclass(frozen=True, slots=True)
class CutoverJournal:
    """Store cutover journal record."""

    path: Path
    state: str
    identity_digest: str
    reset_digest: str
    snapshot_digest: str
    maintenance_digest: str
    digest: str


# Digest helpers


def _compute_identity_digest(
    env_dir: Path,
    services: tuple[ServiceIdentity, ...],
) -> str:
    """Compute identity manifest digest."""
    payload = {
        'env_dir': str(env_dir),
        'services': [
            {
                'service_id': s.service_id,
                'issuer': s.issuer,
                'resource': s.resource,
                'mcp_path': s.mcp_path,
                'port': s.port,
                'state_path': str(s.state_path),
                'google_token_path': str(s.google_token_path),
            }
            for s in services
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _compute_reset_digest(
    state_root: Path,
    identity_digest: str,
    directories: tuple[DirectoryIdentity, ...],
    entries: tuple[ResetEntry, ...],
) -> str:
    """Compute reset manifest digest."""
    payload = {
        'state_root': str(state_root),
        'identity_digest': identity_digest,
        'directories': [
            {
                'service_id': d.service_id,
                'path': str(d.path),
                'device': d.device,
                'inode': d.inode,
                'owner': d.owner,
                'mode': d.mode,
            }
            for d in directories
        ],
        'entries': [
            {
                'path': str(e.path),
                'present': e.present,
                'device': e.device,
                'inode': e.inode,
                'owner': e.owner,
                'mode': e.mode,
                'links': e.links,
                'size': e.size,
                'sha256': e.sha256,
            }
            for e in entries
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _compute_snapshot_digest(
    destination: Path,
    identity_digest: str,
    reset_digest: str,
    entries: tuple[SnapshotEntry, ...],
) -> str:
    """Compute snapshot manifest digest."""
    payload = {
        'destination': str(destination),
        'identity_digest': identity_digest,
        'reset_digest': reset_digest,
        'entries': [
            {
                'service_id': e.service_id,
                'name': e.name,
                'path': str(e.path),
                'present': e.present,
                'size': e.size,
                'sha256': e.sha256,
            }
            for e in entries
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _compute_attestation_digest(
    identity_digest: str,
    nginx_master_pid: int,
    nginx_config_digest: str,
    maintenance_include_target: str,
    worker_generation: int,
) -> str:
    """Compute maintenance attestation digest."""
    payload = {
        'identity_digest': identity_digest,
        'nginx_master_pid': nginx_master_pid,
        'nginx_config_digest': nginx_config_digest,
        'maintenance_include_target': maintenance_include_target,
        'worker_generation': worker_generation,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _compute_journal_digest(
    state: str,
    identity_digest: str,
    reset_digest: str,
    snapshot_digest: str,
    maintenance_digest: str,
) -> str:
    """Compute cutover journal digest."""
    payload = {
        'state': state,
        'identity_digest': identity_digest,
        'reset_digest': reset_digest,
        'snapshot_digest': snapshot_digest,
        'maintenance_digest': maintenance_digest,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


# Secure file helpers


def _write_atomic_secure_file(path: Path, data: bytes) -> None:
    """Write atomic secure file."""
    parent_dir = path.parent
    if not parent_dir.is_absolute():
        raise CutoverError(f'path {path} must be absolute')
    if os.path.islink(parent_dir):
        raise CutoverError(f'parent directory {parent_dir} cannot be symlink')

    try:
        dir_fd = os.open(parent_dir, _DIRECTORY_FLAGS)
    except OSError as err:
        raise CutoverError(
            f'failed to open directory {parent_dir}: {err}'
        ) from err

    try:
        tmp_name = f'.{path.name}.{os.getpid()}.tmp'
        try:
            tmp_fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                mode=0o600,
                dir_fd=dir_fd,
            )
        except OSError as err:
            raise CutoverError(
                f'failed to create temp file {tmp_name}: {err}'
            ) from err

        try:
            total_written = 0
            while total_written < len(data):
                written = os.write(tmp_fd, data[total_written:])
                if written == 0:
                    raise CutoverError(f'failed to write to {tmp_name}')
                total_written += written
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)

        tmp_path = parent_dir / tmp_name
        try:
            os.replace(tmp_path, path)
            os.fsync(dir_fd)
        except OSError as err:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)
            raise CutoverError(
                f'failed to replace {path} atomically: {err}'
            ) from err
    finally:
        os.close(dir_fd)


def _load_secure_json(
    path: Path,
    expected_uid: int = 0,
) -> dict[str, Any]:
    """Load secure json payload."""
    if not path.is_absolute():
        raise CutoverError(f'path {path} must be absolute')
    if os.path.islink(path):
        raise CutoverError(f'file {path} cannot be symlink')

    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as err:
        raise CutoverError(f'failed to open {path}: {err}') from err

    try:
        st_before = os.fstat(fd)
        if not stat.S_ISREG(st_before.st_mode):
            raise CutoverError(f'file {path} is not regular')
        if st_before.st_nlink != 1:
            raise CutoverError(f'file {path} has multiple links')
        if expected_uid is not None and st_before.st_uid != expected_uid:
            raise CutoverError(
                f'file {path} owned by {st_before.st_uid}, '
                f'expected {expected_uid}'
            )
        if (st_before.st_mode & 0o077) != 0:
            raise CutoverError(
                f'file {path} permissions {oct(st_before.st_mode)} too open'
            )

        content_bytes = bytearray()
        while chunk := os.read(fd, 65536):
            content_bytes.extend(chunk)

        st_after = os.fstat(fd)
        if (
            st_before.st_dev != st_after.st_dev
            or st_before.st_ino != st_after.st_ino
            or st_before.st_size != st_after.st_size
        ):
            raise CutoverError(f'file {path} modified during read')
    finally:
        os.close(fd)

    try:
        parsed = json.loads(content_bytes.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise CutoverError(f'failed to parse json in {path}: {err}') from err

    if not isinstance(parsed, dict):
        raise CutoverError(f'json payload in {path} must be object')
    return parsed


def _validate_root_provenance_directory(
    path: Path,
    expected_uid: int,
) -> int:
    """Open verified directory descriptor."""
    if not path.is_absolute():
        raise CutoverError(f'path {path} must be absolute')
    if os.path.islink(path):
        raise CutoverError(f'directory {path} cannot be a symlink')
    try:
        dir_fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError as err:
        raise CutoverError(f'failed to open directory {path}: {err}') from err

    st = os.fstat(dir_fd)
    if not stat.S_ISDIR(st.st_mode):
        os.close(dir_fd)
        raise CutoverError(f'{path} is not a regular directory')
    if st.st_uid != expected_uid:
        os.close(dir_fd)
        raise CutoverError(
            f'directory {path} owned by uid {st.st_uid}, '
            f'expected {expected_uid}'
        )
    if (st.st_mode & 0o077) != 0:
        os.close(dir_fd)
        raise CutoverError(
            f'directory {path} has permissions {oct(st.st_mode)}, '
            'expected 0700 or stricter'
        )
    return dir_fd


# Identity manifest


def build_identity_manifest(
    env_dir: Path,
    expected_uid: int = 0,
) -> IdentityManifest:
    """Build identity manifest record."""
    dir_fd = _validate_root_provenance_directory(env_dir, expected_uid)
    services_list: list[ServiceIdentity] = []

    try:
        for service in SERVICES:
            filename = f'{service}.env'
            try:
                fd = os.open(filename, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd)
            except OSError as err:
                raise CutoverError(
                    f'failed to open env file {filename} '
                    f'(symlink or insecure provenance): {err}'
                ) from err

            try:
                st_before = os.fstat(fd)
                if not stat.S_ISREG(st_before.st_mode):
                    raise CutoverError(f'env file {filename} is not regular')
                if st_before.st_nlink != 1:
                    raise CutoverError(f'env file {filename} has hard links')
                if st_before.st_uid != expected_uid:
                    raise CutoverError(
                        f'env file {filename} owned by {st_before.st_uid}, '
                        f'expected {expected_uid}'
                    )
                if (st_before.st_mode & 0o077) != 0:
                    raise CutoverError(
                        f'env file {filename} permissions '
                        f'{oct(st_before.st_mode)} too open'
                    )

                content_bytes = os.read(fd, 65536)
                st_after = os.fstat(fd)
                if (
                    st_before.st_dev != st_after.st_dev
                    or st_before.st_ino != st_after.st_ino
                    or st_before.st_size != st_after.st_size
                ):
                    raise CutoverError(
                        f'env file {filename} modified during read'
                    )
            finally:
                os.close(fd)

            content = content_bytes.decode('utf-8')
            svc_upper = service.upper()
            required_keys = {
                f'{svc_upper}_MCP_PUBLIC_URL',
                f'{svc_upper}_MCP_PATH',
                f'{svc_upper}_MCP_PORT',
                f'{svc_upper}_OAUTH_STATE_PATH',
                f'{svc_upper}_GOOGLE_TOKEN_PATH',
            }
            parsed_kv: dict[str, str] = {}
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                if any(c in line for c in ('$', '`', '(', ')')):
                    raise CutoverError(
                        f'shell expansion syntax in {filename}: {line}'
                    )
                if '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip().strip('\'"')
                if key in parsed_kv:
                    raise CutoverError(f'duplicate key {key} in {filename}')
                parsed_kv[key] = val

            missing = required_keys - set(parsed_kv.keys())
            if missing:
                raise CutoverError(
                    f'missing required key(s) in {filename}: {missing}'
                )

            public_url = parsed_kv[f'{svc_upper}_MCP_PUBLIC_URL']
            mcp_path = parsed_kv[f'{svc_upper}_MCP_PATH']
            port_str = parsed_kv[f'{svc_upper}_MCP_PORT']
            state_path_str = parsed_kv[f'{svc_upper}_OAUTH_STATE_PATH']
            google_token_path_str = parsed_kv[f'{svc_upper}_GOOGLE_TOKEN_PATH']

            try:
                resource_url = _validate_service_identity(
                    service, public_url, mcp_path
                )
            except ValueError as err:
                raise CutoverError(
                    f'invalid service identity in {filename}: {err}'
                ) from err

            if mcp_path != f'/{service}/mcp':
                raise CutoverError(
                    f'invalid mcp_path {mcp_path} for service {service}'
                )

            try:
                port = int(port_str)
                if not (1 <= port <= 65535):
                    raise ValueError('out of range')
            except ValueError as err:
                raise CutoverError(
                    f'invalid port {port_str} in {filename}: {err}'
                ) from err

            state_path = Path(state_path_str)
            if (
                not state_path.is_absolute()
                or state_path.name != 'oauth_state.sqlite3'
                or state_path.parent.name != service
            ):
                raise CutoverError(
                    f'invalid state path {state_path} in {filename}'
                )

            token_path = Path(google_token_path_str)
            if (
                not token_path.is_absolute()
                or token_path.name != 'google_token.json'
                or token_path.parent.name != service
            ):
                raise CutoverError(
                    f'invalid token path {token_path} in {filename}'
                )

            if state_path == token_path:
                raise CutoverError(
                    f'state path and token path collision in {filename}'
                )

            services_list.append(
                ServiceIdentity(
                    service_id=service,
                    issuer=public_url,
                    resource=resource_url,
                    mcp_path=mcp_path,
                    port=port,
                    state_path=state_path,
                    google_token_path=token_path,
                )
            )
    finally:
        os.close(dir_fd)

    # Validate pairwise uniqueness.
    if len({s.service_id for s in services_list}) != 5:
        raise CutoverError('duplicate service_id in identity manifest')
    if len({s.issuer for s in services_list}) != 5:
        raise CutoverError('duplicate issuer in identity manifest')
    if len({s.resource for s in services_list}) != 5:
        raise CutoverError('duplicate resource in identity manifest')
    if len({s.mcp_path for s in services_list}) != 5:
        raise CutoverError('duplicate mcp_path in identity manifest')
    if len({s.state_path for s in services_list}) != 5:
        raise CutoverError('duplicate state_path in identity manifest')
    if len({s.google_token_path for s in services_list}) != 5:
        raise CutoverError('duplicate google_token_path in identity manifest')
    if len({s.port for s in services_list}) != 5:
        raise CutoverError('duplicate port in identity manifest')

    all_paths = [s.state_path for s in services_list] + [
        s.google_token_path for s in services_list
    ]
    if len(set(all_paths)) != 10:
        raise CutoverError('intersecting state and token paths detected')

    services_tuple = tuple(services_list)
    digest = _compute_identity_digest(env_dir, services_tuple)
    return IdentityManifest(
        env_dir=env_dir,
        services=services_tuple,
        digest=digest,
    )


# Systemd and quiescence checks


def _is_unit_masked(unit_name: str) -> bool:
    """Check unit mask state."""
    mask_locations = [
        Path(f'/run/systemd/system/{unit_name}'),
        Path(f'/etc/systemd/system/{unit_name}'),
    ]
    for loc in mask_locations:
        if loc.is_symlink() and str(loc.readlink()) == '/dev/null':
            return True
    return False


def _check_no_activation_units(identity: IdentityManifest) -> None:
    """Check activation unit absence."""
    unit_bases = ['google-mcp@', 'google-mcp'] + [
        f'google-mcp@{s.service_id}' for s in identity.services
    ]
    suffixes = ('.socket', '.path', '.timer')
    search_dirs = [
        Path('/etc/systemd/system'),
        Path('/run/systemd/system'),
        Path('/lib/systemd/system'),
        Path('/usr/lib/systemd/system'),
    ]
    for base in unit_bases:
        for suffix in suffixes:
            unit_name = f'{base}{suffix}'
            for sdir in search_dirs:
                unit_path = sdir / unit_name
                if unit_path.exists() or unit_path.is_symlink():
                    if (
                        unit_path.is_symlink()
                        and str(unit_path.readlink()) == '/dev/null'
                    ):
                        continue
                    raise CutoverError(
                        f'activation unit {unit_name} exists at {unit_path} '
                        'and is not masked to /dev/null'
                    )


def assert_mutation_exclusion(identity: IdentityManifest) -> None:
    """Verify runtime mutation exclusion."""
    # Check template unit.
    template = 'google-mcp@.service'
    if not _is_unit_masked(template):
        raise CutoverError(
            f'template unit {template} must be runtime masked to /dev/null'
        )

    # Check 5 instance units.
    for service in identity.services:
        unit = f'google-mcp@{service.service_id}.service'
        if not _is_unit_masked(unit):
            raise CutoverError(
                f'unit {unit} must be runtime masked to /dev/null'
            )

    # Check companion activation units.
    _check_no_activation_units(identity)


def _check_systemd_unit_inactive(unit_name: str) -> None:
    """Check systemd unit state."""
    try:
        proc = subprocess.run(
            ['systemctl', 'is-active', unit_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError) as err:
        raise CutoverError(
            f'failed to check systemd status for {unit_name}: {err}'
        ) from err

    output = (proc.stdout or '').strip().lower()
    if proc.returncode == 0 or output in {
        'active',
        'activating',
        'reloading',
        'deactivating',
    }:
        raise CutoverError(
            f'systemd unit {unit_name} is active or activating ({output})'
        )


def _check_proc_net_tcp(
    proc_path: Path,
    forbidden_ports: set[int],
    allow_non_linux: bool = False,
) -> None:
    """Check tcp sockets table."""
    if not proc_path.exists():
        if sys.platform == 'linux' and not allow_non_linux:
            raise CutoverError(
                f'required proc table {proc_path} does not exist on Linux'
            )
        return
    try:
        content = proc_path.read_text(encoding='utf-8')
    except OSError as err:
        raise CutoverError(
            f'failed to read proc table {proc_path}: {err}'
        ) from err

    for line in content.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        local_addr = parts[1]
        state = parts[3]
        if state.upper() == '0A' and ':' in local_addr:
            hex_port = local_addr.split(':', 1)[1]
            try:
                port = int(hex_port, 16)
                if port in forbidden_ports:
                    raise CutoverError(
                        f'port {port} is active in LISTEN state in {proc_path}'
                    )
            except ValueError:
                continue


def _check_network_namespace_is_host() -> None:
    """Verify host network namespace."""
    self_ns = Path('/proc/self/ns/net')
    init_ns = Path('/proc/1/ns/net')
    if self_ns.exists() and init_ns.exists():
        try:
            if os.readlink(self_ns) != os.readlink(init_ns):
                raise CutoverError(
                    'quiescence check must run in host network namespace'
                )
        except OSError:
            pass


def assert_services_quiescent(
    identity: IdentityManifest,
    proc_net_tcp_path: Path = Path('/proc/net/tcp'),
    proc_net_tcp6_path: Path = Path('/proc/net/tcp6'),
    skip_systemd_check: bool = False,
    allow_non_linux: bool = False,
) -> None:
    """Verify quiescence of services."""
    if not skip_systemd_check:
        _check_systemd_unit_inactive('google-mcp@.service')
        for service in identity.services:
            _check_systemd_unit_inactive(
                f'google-mcp@{service.service_id}.service'
            )

    forbidden_ports = {s.port for s in identity.services}

    # Check TCP4 and TCP6 tables.
    _check_proc_net_tcp(
        proc_net_tcp_path, forbidden_ports, allow_non_linux=allow_non_linux
    )
    _check_proc_net_tcp(
        proc_net_tcp6_path, forbidden_ports, allow_non_linux=allow_non_linux
    )

    # Check host network namespace if available.
    _check_network_namespace_is_host()


# Reset manifest


def build_reset_manifest(
    identity: IdentityManifest,
    state_root: Path,
    expected_uid: int = 0,
) -> ResetManifest:
    """Build reset manifest record."""
    root_fd = _validate_root_provenance_directory(state_root, expected_uid)
    os.close(root_fd)

    directories: list[DirectoryIdentity] = []
    entries: list[ResetEntry] = []

    for service in identity.services:
        svc_dir = service.state_path.parent
        if not svc_dir.is_relative_to(state_root):
            raise CutoverError(
                f'service state path {service.state_path} '
                f'outside state root {state_root}'
            )
        if svc_dir.name != service.service_id:
            raise CutoverError(
                f'service directory {svc_dir.name} does not match '
                f'service_id {service.service_id}'
            )

        dir_fd = _validate_root_provenance_directory(svc_dir, expected_uid)
        try:
            st_dir = os.fstat(dir_fd)
            directories.append(
                DirectoryIdentity(
                    service_id=service.service_id,
                    path=svc_dir,
                    device=st_dir.st_dev,
                    inode=st_dir.st_ino,
                    owner=st_dir.st_uid,
                    mode=st_dir.st_mode,
                )
            )

            # Check for rogue files with oauth_state.sqlite3 prefix.
            dir_entries = os.listdir(dir_fd)
            for name in dir_entries:
                if (
                    name.startswith('oauth_state.sqlite3')
                    and name not in AUTHORITATIVE_STATE_NAMES
                ):
                    raise CutoverError(
                        f'unrecognized oauth_state artifact: {name} '
                        f'in {svc_dir}'
                    )

            # Enumerate authoritative artifacts.
            for name in sorted(AUTHORITATIVE_STATE_NAMES):
                entry_path = svc_dir / name
                try:
                    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd)
                except FileNotFoundError:
                    entries.append(
                        ResetEntry(
                            path=entry_path,
                            present=False,
                            device=None,
                            inode=None,
                            owner=None,
                            mode=None,
                            links=None,
                            size=None,
                            sha256=None,
                        )
                    )
                    continue
                except OSError as err:
                    raise CutoverError(
                        f'failed to open entry {name} in {svc_dir}: {err}'
                    ) from err

                try:
                    st_before = os.fstat(fd)
                    if not stat.S_ISREG(st_before.st_mode):
                        raise CutoverError(
                            f'entry {entry_path} is not regular file'
                        )
                    if st_before.st_nlink != 1:
                        raise CutoverError(
                            f'entry {entry_path} has hard links'
                        )
                    if st_before.st_uid != expected_uid:
                        raise CutoverError(
                            f'entry {entry_path} owned by '
                            f'{st_before.st_uid}, expected {expected_uid}'
                        )
                    if (st_before.st_mode & 0o077) != 0:
                        raise CutoverError(
                            f'entry {entry_path} permissions '
                            f'{oct(st_before.st_mode)} too open'
                        )

                    hasher = hashlib.sha256()
                    while chunk := os.read(fd, 65536):
                        hasher.update(chunk)
                    st_after = os.fstat(fd)
                    if (
                        st_before.st_dev != st_after.st_dev
                        or st_before.st_ino != st_after.st_ino
                        or st_before.st_size != st_after.st_size
                    ):
                        raise CutoverError(
                            f'entry {entry_path} modified during hash'
                        )

                    entries.append(
                        ResetEntry(
                            path=entry_path,
                            present=True,
                            device=st_before.st_dev,
                            inode=st_before.st_ino,
                            owner=st_before.st_uid,
                            mode=st_before.st_mode,
                            links=st_before.st_nlink,
                            size=st_before.st_size,
                            sha256=hasher.hexdigest(),
                        )
                    )
                finally:
                    os.close(fd)
        finally:
            os.close(dir_fd)

    directories_tuple = tuple(directories)
    entries_tuple = tuple(entries)
    digest = _compute_reset_digest(
        state_root, identity.digest, directories_tuple, entries_tuple
    )
    return ResetManifest(
        state_root=state_root,
        identity_digest=identity.digest,
        directories=directories_tuple,
        entries=entries_tuple,
        digest=digest,
    )


# Reset apply


def apply_reset_manifest(
    identity: IdentityManifest,
    manifest: ResetManifest,
    allow_non_linux: bool = False,
    expected_env_uid: int = 0,
) -> None:
    """Apply reset manifest safely."""
    if sys.platform != 'linux' and not allow_non_linux:
        raise CutoverError('reset apply requires Linux platform')

    # Pre-checks.
    assert_mutation_exclusion(identity)
    assert_services_quiescent(
        identity,
        allow_non_linux=allow_non_linux,
        skip_systemd_check=allow_non_linux,
    )

    # Rebuild live identity manifest to verify unchanged digest.
    rebuilt_identity = build_identity_manifest(
        identity.env_dir,
        expected_uid=expected_env_uid,
    )
    if rebuilt_identity.digest != manifest.identity_digest:
        raise CutoverError(
            'live identity digest does not match reset manifest'
        )

    # Phase 1: Descriptor-relative preflight and verification.
    dir_fds: dict[str, int] = {}
    try:
        for d in manifest.directories:
            fd = os.open(d.path, _DIRECTORY_FLAGS)
            st = os.fstat(fd)
            if (
                st.st_dev != d.device
                or st.st_ino != d.inode
                or st.st_uid != d.owner
                or st.st_mode != d.mode
            ):
                raise CutoverError(
                    f'directory {d.path} changed device/inode/owner/mode'
                )
            dir_fds[d.service_id] = fd

            # Check rogue files.
            for name in os.listdir(fd):
                if (
                    name.startswith('oauth_state.sqlite3')
                    and name not in AUTHORITATIVE_STATE_NAMES
                ):
                    raise CutoverError(
                        f'unrecognized artifact {name} appeared in {d.path}'
                    )

        for entry in manifest.entries:
            svc_id = entry.path.parent.name
            dfd = dir_fds[svc_id]
            name = entry.path.name

            if entry.present:
                try:
                    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dfd)
                except OSError as err:
                    raise CutoverError(
                        f'manifest entry {entry.path} missing '
                        f'or inaccessible: {err}'
                    ) from err

                try:
                    st_before = os.fstat(fd)
                    if (
                        st_before.st_dev != entry.device
                        or st_before.st_ino != entry.inode
                        or st_before.st_uid != entry.owner
                        or st_before.st_mode != entry.mode
                        or st_before.st_nlink != entry.links
                        or st_before.st_size != entry.size
                    ):
                        raise CutoverError(
                            f'manifest entry changed metadata: {entry.path}'
                        )

                    hasher = hashlib.sha256()
                    while chunk := os.read(fd, 65536):
                        hasher.update(chunk)
                    st_after = os.fstat(fd)
                    if (
                        st_before.st_dev != st_after.st_dev
                        or st_before.st_ino != st_after.st_ino
                        or st_before.st_size != st_after.st_size
                    ):
                        raise CutoverError(
                            f'manifest entry changed during hashing: '
                            f'{entry.path}'
                        )
                    if hasher.hexdigest() != entry.sha256:
                        raise CutoverError(
                            f'manifest entry changed content sha256: '
                            f'{entry.path}'
                        )
                finally:
                    os.close(fd)
            else:
                # Must still be absent.
                try:
                    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dfd)
                    os.close(fd)
                    raise CutoverError(
                        f'manifest entry changed: previously absent file '
                        f'appeared: {entry.path}'
                    )
                except FileNotFoundError:
                    pass

        # Repeat mutation exclusion and quiescence immediately before Phase 2.
        assert_mutation_exclusion(identity)
        assert_services_quiescent(
            identity,
            allow_non_linux=allow_non_linux,
            skip_systemd_check=allow_non_linux,
        )

        # Phase 2: Descriptor-relative mutation.
        for entry in manifest.entries:
            if entry.present:
                svc_id = entry.path.parent.name
                dfd = dir_fds[svc_id]
                try:
                    os.unlink(entry.path.name, dir_fd=dfd)
                except OSError as err:
                    raise CutoverError(
                        f'failed to unlink {entry.path}: {err}'
                    ) from err

        for dfd in dir_fds.values():
            try:
                os.fsync(dfd)
            except OSError as err:
                raise CutoverError(
                    f'failed to fsync directory descriptor: {err}'
                ) from err
    finally:
        for dfd in dir_fds.values():
            with contextlib.suppress(OSError):
                os.close(dfd)


# Maintenance attestation


def create_maintenance_attestation(
    identity: IdentityManifest,
    path: Path,
    nginx_master_pid: int,
    nginx_config_digest: str,
    maintenance_include_target: str,
    worker_generation: int,
) -> MaintenanceAttestation:
    """Create maintenance attestation record."""
    digest = _compute_attestation_digest(
        identity.digest,
        nginx_master_pid,
        nginx_config_digest,
        maintenance_include_target,
        worker_generation,
    )
    attestation = MaintenanceAttestation(
        identity_digest=identity.digest,
        nginx_master_pid=nginx_master_pid,
        nginx_config_digest=nginx_config_digest,
        maintenance_include_target=maintenance_include_target,
        worker_generation=worker_generation,
        digest=digest,
    )

    payload = asdict(attestation)
    data = json.dumps(payload, sort_keys=True, indent=2).encode('utf-8')
    _write_atomic_secure_file(path, data)
    return attestation


def verify_maintenance_attestation(
    identity: IdentityManifest,
    attestation: MaintenanceAttestation,
    expected_nginx_master_pid: int | None = None,
    expected_nginx_config_digest: str | None = None,
    expected_include_target: str | None = None,
    expected_worker_generation: int | None = None,
) -> None:
    """Verify maintenance attestation record."""
    recomputed = _compute_attestation_digest(
        attestation.identity_digest,
        attestation.nginx_master_pid,
        attestation.nginx_config_digest,
        attestation.maintenance_include_target,
        attestation.worker_generation,
    )
    if recomputed != attestation.digest:
        raise CutoverError('maintenance attestation digest mismatch')
    if attestation.identity_digest != identity.digest:
        raise CutoverError(
            'maintenance attestation identity digest does not match'
        )
    if (
        expected_nginx_master_pid is not None
        and attestation.nginx_master_pid != expected_nginx_master_pid
    ):
        raise CutoverError('maintenance attestation master pid mismatch')
    if (
        expected_nginx_config_digest is not None
        and attestation.nginx_config_digest != expected_nginx_config_digest
    ):
        raise CutoverError('maintenance attestation config digest mismatch')
    if (
        expected_include_target is not None
        and attestation.maintenance_include_target != expected_include_target
    ):
        raise CutoverError('maintenance attestation include target mismatch')
    if (
        expected_worker_generation is not None
        and attestation.worker_generation != expected_worker_generation
    ):
        raise CutoverError(
            'maintenance attestation worker generation mismatch'
        )


# Offline snapshot


def create_offline_snapshot(
    identity: IdentityManifest,
    reset: ResetManifest,
    destination: Path,
    expected_uid: int = 0,
    expected_env_uid: int = 0,
    allow_non_linux: bool = False,
) -> SnapshotManifest:
    """Create offline snapshot record."""
    if sys.platform != 'linux' and not allow_non_linux:
        raise CutoverError('snapshot create requires Linux platform')

    assert_services_quiescent(
        identity,
        allow_non_linux=allow_non_linux,
        skip_systemd_check=allow_non_linux,
    )

    # Rebuild live identity manifest to verify unchanged digest.
    rebuilt_identity = build_identity_manifest(
        identity.env_dir,
        expected_uid=expected_env_uid,
    )
    if rebuilt_identity.digest != identity.digest:
        raise CutoverError(
            'live identity digest does not match identity manifest'
        )
    if reset.identity_digest != identity.digest:
        raise CutoverError(
            'reset manifest identity digest does not match identity manifest'
        )

    dest_fd = _validate_root_provenance_directory(destination, expected_uid)
    os.close(dest_fd)

    snapshot_entries: list[SnapshotEntry] = []

    for service in identity.services:
        svc_id = service.service_id
        dest_svc_dir = destination / svc_id
        dest_svc_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(dest_svc_dir, 0o700)

        svc_dir = service.state_path.parent
        src_dir_fd = os.open(svc_dir, _DIRECTORY_FLAGS)
        dest_dir_fd = os.open(dest_svc_dir, _DIRECTORY_FLAGS)
        try:
            for entry in reset.entries:
                if entry.path.parent.name != svc_id:
                    continue
                name = entry.path.name
                dest_file_path = dest_svc_dir / name
                if entry.present:
                    src_fd = os.open(
                        name, os.O_RDONLY | _NOFOLLOW, dir_fd=src_dir_fd
                    )
                    try:
                        dest_fd = os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW,
                            mode=0o600,
                            dir_fd=dest_dir_fd,
                        )
                        try:
                            hasher = hashlib.sha256()
                            size = 0
                            while chunk := os.read(src_fd, 65536):
                                os.write(dest_fd, chunk)
                                hasher.update(chunk)
                                size += len(chunk)
                            os.fsync(dest_fd)
                            snapshot_entries.append(
                                SnapshotEntry(
                                    service_id=svc_id,
                                    name=name,
                                    path=dest_file_path,
                                    present=True,
                                    size=size,
                                    sha256=hasher.hexdigest(),
                                )
                            )
                        finally:
                            os.close(dest_fd)
                    finally:
                        os.close(src_fd)
                else:
                    snapshot_entries.append(
                        SnapshotEntry(
                            service_id=svc_id,
                            name=name,
                            path=dest_file_path,
                            present=False,
                            size=None,
                            sha256=None,
                        )
                    )
            os.fsync(dest_dir_fd)
        finally:
            os.close(src_dir_fd)
            os.close(dest_dir_fd)

    entries_tuple = tuple(snapshot_entries)
    digest = _compute_snapshot_digest(
        destination, identity.digest, reset.digest, entries_tuple
    )
    manifest = SnapshotManifest(
        destination=destination,
        identity_digest=identity.digest,
        reset_digest=reset.digest,
        entries=entries_tuple,
        digest=digest,
    )

    # Write manifest file and completion marker using atomic helper.
    manifest_file = destination / 'snapshot_manifest.json'
    manifest_data = {
        'destination': str(destination),
        'identity_digest': manifest.identity_digest,
        'reset_digest': manifest.reset_digest,
        'entries': [
            {
                'service_id': e.service_id,
                'name': e.name,
                'path': str(e.path),
                'present': e.present,
                'size': e.size,
                'sha256': e.sha256,
            }
            for e in manifest.entries
        ],
        'digest': manifest.digest,
    }
    _write_atomic_secure_file(
        manifest_file,
        json.dumps(manifest_data, sort_keys=True, indent=2).encode('utf-8'),
    )

    complete_marker = destination / '.complete'
    _write_atomic_secure_file(complete_marker, b'OK\n')

    # Fsync destination root.
    root_dfd = os.open(destination, _DIRECTORY_FLAGS)
    try:
        os.fsync(root_dfd)
    finally:
        os.close(root_dfd)

    return manifest


def verify_offline_snapshot(
    snapshot: SnapshotManifest,
    expected_uid: int = 0,
) -> None:
    """Verify offline snapshot record."""
    if not snapshot.destination.is_absolute():
        raise CutoverError(
            f'destination path {snapshot.destination} must be absolute'
        )
    if os.path.islink(snapshot.destination):
        raise CutoverError(
            f'destination {snapshot.destination} cannot be symlink'
        )

    try:
        dest_dfd = os.open(snapshot.destination, _DIRECTORY_FLAGS)
    except OSError as err:
        raise CutoverError(
            f'failed to open destination {snapshot.destination}: {err}'
        ) from err

    try:
        # Verify .complete marker descriptor-relatively.
        try:
            mfd = os.open(
                '.complete', os.O_RDONLY | _NOFOLLOW, dir_fd=dest_dfd
            )
        except OSError as err:
            raise CutoverError(
                f'snapshot missing completion marker: {err}'
            ) from err

        try:
            mst = os.fstat(mfd)
            if not stat.S_ISREG(mst.st_mode):
                raise CutoverError('completion marker is not regular file')
            if mst.st_nlink != 1:
                raise CutoverError('completion marker has multiple links')
            if expected_uid is not None and mst.st_uid != expected_uid:
                raise CutoverError(
                    f'completion marker owned by {mst.st_uid}, '
                    f'expected {expected_uid}'
                )
            if (mst.st_mode & 0o077) != 0:
                raise CutoverError('completion marker permissions too open')
            marker_bytes = os.read(mfd, 1024)
            if marker_bytes.strip() != b'OK':
                raise CutoverError('completion marker content invalid')
        finally:
            os.close(mfd)

        # Verify all entries descriptor-relatively.
        for svc_id in SERVICES:
            try:
                svc_dfd = os.open(svc_id, _DIRECTORY_FLAGS, dir_fd=dest_dfd)
            except OSError as err:
                raise CutoverError(
                    f'snapshot service directory {svc_id} inaccessible: {err}'
                ) from err

            try:
                for entry in snapshot.entries:
                    if entry.service_id != svc_id:
                        continue
                    name = entry.name
                    if entry.present:
                        try:
                            efd = os.open(
                                name, os.O_RDONLY | _NOFOLLOW, dir_fd=svc_dfd
                            )
                        except OSError as err:
                            raise CutoverError(
                                f'snapshot entry {name} missing in {svc_id}: '
                                f'{err}'
                            ) from err

                        try:
                            est_before = os.fstat(efd)
                            if not stat.S_ISREG(est_before.st_mode):
                                raise CutoverError(
                                    f'snapshot entry {name} is not regular'
                                )
                            if est_before.st_nlink != 1:
                                raise CutoverError(
                                    f'snapshot entry {name} has multiple links'
                                )
                            if (
                                expected_uid is not None
                                and est_before.st_uid != expected_uid
                            ):
                                raise CutoverError(
                                    f'snapshot entry {name} owned by '
                                    f'{est_before.st_uid}, '
                                    f'expected {expected_uid}'
                                )
                            if (est_before.st_mode & 0o077) != 0:
                                raise CutoverError(
                                    f'snapshot entry {name} permissions '
                                    'too open'
                                )
                            if est_before.st_size != entry.size:
                                raise CutoverError(
                                    f'snapshot entry {name} size mismatch'
                                )

                            hasher = hashlib.sha256()
                            while chunk := os.read(efd, 65536):
                                hasher.update(chunk)
                            est_after = os.fstat(efd)
                            if (
                                est_before.st_dev != est_after.st_dev
                                or est_before.st_ino != est_after.st_ino
                                or est_before.st_size != est_after.st_size
                            ):
                                raise CutoverError(
                                    f'snapshot entry {name} modified '
                                    'during verification'
                                )
                            if hasher.hexdigest() != entry.sha256:
                                raise CutoverError(
                                    f'snapshot entry {name} sha256 mismatch'
                                )
                        finally:
                            os.close(efd)
                    else:
                        try:
                            efd = os.open(
                                name, os.O_RDONLY | _NOFOLLOW, dir_fd=svc_dfd
                            )
                            os.close(efd)
                            raise CutoverError(
                                f'absent snapshot entry {name} present '
                                f'in {svc_id}'
                            )
                        except FileNotFoundError:
                            pass
            finally:
                os.close(svc_dfd)
    finally:
        os.close(dest_dfd)


# Cutover journal


def create_cutover_journal(
    identity: IdentityManifest,
    reset: ResetManifest,
    snapshot: SnapshotManifest,
    maintenance: MaintenanceAttestation,
    path: Path,
) -> CutoverJournal:
    """Create cutover journal record."""
    if reset.identity_digest != identity.digest:
        raise CutoverError('reset manifest identity digest mismatch')
    if snapshot.identity_digest != identity.digest:
        raise CutoverError('snapshot manifest identity digest mismatch')
    if snapshot.reset_digest != reset.digest:
        raise CutoverError('snapshot manifest reset digest mismatch')
    if maintenance.identity_digest != identity.digest:
        raise CutoverError('maintenance attestation identity digest mismatch')

    state = 'PRE_GATE'
    digest = _compute_journal_digest(
        state,
        identity.digest,
        reset.digest,
        snapshot.digest,
        maintenance.digest,
    )
    journal = CutoverJournal(
        path=path,
        state=state,
        identity_digest=identity.digest,
        reset_digest=reset.digest,
        snapshot_digest=snapshot.digest,
        maintenance_digest=maintenance.digest,
        digest=digest,
    )

    payload = asdict(journal)
    payload['path'] = str(journal.path)
    data = json.dumps(payload, sort_keys=True, indent=2).encode('utf-8')
    _write_atomic_secure_file(path, data)
    return journal


def mark_gate_opened(journal: CutoverJournal) -> CutoverJournal:
    """Transition cutover journal state."""
    if journal.state != 'PRE_GATE':
        raise CutoverError(
            f'cannot open gate from state {journal.state}, expected PRE_GATE'
        )

    new_state = 'GATE_OPENED'
    new_digest = _compute_journal_digest(
        new_state,
        journal.identity_digest,
        journal.reset_digest,
        journal.snapshot_digest,
        journal.maintenance_digest,
    )
    new_journal = CutoverJournal(
        path=journal.path,
        state=new_state,
        identity_digest=journal.identity_digest,
        reset_digest=journal.reset_digest,
        snapshot_digest=journal.snapshot_digest,
        maintenance_digest=journal.maintenance_digest,
        digest=new_digest,
    )
    payload = asdict(new_journal)
    payload['path'] = str(new_journal.path)
    data = json.dumps(payload, sort_keys=True, indent=2).encode('utf-8')
    _write_atomic_secure_file(journal.path, data)
    return new_journal


def reconcile_gate(journal: CutoverJournal) -> str:
    """Reconcile cutover gate status."""
    if journal.state == 'PRE_GATE':
        return 'enforce_maintenance'
    if journal.state == 'GATE_OPENED':
        return 'enforce_active_roll_forward'
    raise CutoverError(f'unknown journal state {journal.state}')


# Snapshot restore


def restore_offline_snapshot(
    identity: IdentityManifest,
    snapshot: SnapshotManifest,
    journal: CutoverJournal,
    maintenance: MaintenanceAttestation,
    state_root: Path,
    expected_uid: int = 0,
    expected_env_uid: int = 0,
    allow_non_linux: bool = False,
) -> None:
    """Restore offline snapshot state."""
    if sys.platform != 'linux' and not allow_non_linux:
        raise CutoverError('snapshot restore requires Linux platform')

    if journal.state == 'GATE_OPENED':
        raise CutoverError(
            'restore refused: gate has been opened, '
            'only roll forward permitted'
        )
    if journal.state != 'PRE_GATE':
        raise CutoverError(
            f'restore refused: invalid journal state {journal.state}'
        )

    # Rebuild live identity manifest to verify unchanged digest.
    rebuilt_identity = build_identity_manifest(
        identity.env_dir,
        expected_uid=expected_env_uid,
    )
    if rebuilt_identity.digest != identity.digest:
        raise CutoverError(
            'restore refused: live identity digest does not match'
        )

    if (
        identity.digest != snapshot.identity_digest
        or identity.digest != journal.identity_digest
    ):
        raise CutoverError('restore refused: identity digest mismatch')
    if snapshot.digest != journal.snapshot_digest:
        raise CutoverError('restore refused: snapshot digest mismatch')
    if maintenance.digest != journal.maintenance_digest:
        raise CutoverError('restore refused: maintenance digest mismatch')

    verify_maintenance_attestation(identity, maintenance)
    verify_offline_snapshot(snapshot, expected_uid=expected_env_uid)
    assert_mutation_exclusion(identity)
    assert_services_quiescent(
        identity,
        allow_non_linux=allow_non_linux,
        skip_systemd_check=allow_non_linux,
    )

    # Phase 1: All-service source preflight before touching destination disk.
    dest_dir_fds: dict[str, int] = {}
    src_dir_fds: dict[str, int] = {}
    try:
        # Prevalidate destination state directories.
        for service in identity.services:
            svc_id = service.service_id
            svc_dir = service.state_path.parent
            if not svc_dir.is_relative_to(state_root):
                raise CutoverError(
                    f'service state path {service.state_path} '
                    f'outside state root {state_root}'
                )
            if svc_dir.name != svc_id:
                raise CutoverError(
                    f'service directory {svc_dir.name} does not match '
                    f'service_id {svc_id}'
                )

            dest_fd = _validate_root_provenance_directory(
                svc_dir, expected_uid
            )
            try:
                for name in os.listdir(dest_fd):
                    if (
                        name.startswith('oauth_state.sqlite3')
                        and name not in AUTHORITATIVE_STATE_NAMES
                    ):
                        raise CutoverError(
                            f'unrecognized artifact {name} in '
                            f'destination {svc_dir}'
                        )
            finally:
                os.close(dest_fd)

            dest_dir_fds[svc_id] = os.open(svc_dir, _DIRECTORY_FLAGS)
            src_svc_dir = snapshot.destination / svc_id
            src_dir_fds[svc_id] = os.open(src_svc_dir, _DIRECTORY_FLAGS)

        # Prevalidate all snapshot source files across all services.
        for entry in snapshot.entries:
            if not entry.present:
                continue
            s_dfd = src_dir_fds[entry.service_id]
            try:
                sfd = os.open(
                    entry.name, os.O_RDONLY | _NOFOLLOW, dir_fd=s_dfd
                )
            except OSError as err:
                raise CutoverError(
                    f'snapshot source entry {entry.name} in '
                    f'{entry.service_id} inaccessible: {err}'
                ) from err
            try:
                st_before = os.fstat(sfd)
                if not stat.S_ISREG(st_before.st_mode):
                    raise CutoverError(
                        f'snapshot source {entry.name} is not regular'
                    )
                if st_before.st_nlink != 1:
                    raise CutoverError(
                        f'snapshot source {entry.name} has multiple links'
                    )
                if (st_before.st_mode & 0o077) != 0:
                    raise CutoverError(
                        f'snapshot source {entry.name} permissions too open'
                    )
                if st_before.st_size != entry.size:
                    raise CutoverError(
                        f'snapshot source {entry.name} size mismatch'
                    )

                hasher = hashlib.sha256()
                while chunk := os.read(sfd, 65536):
                    hasher.update(chunk)
                st_after = os.fstat(sfd)
                if (
                    st_before.st_dev != st_after.st_dev
                    or st_before.st_ino != st_after.st_ino
                    or st_before.st_size != st_after.st_size
                ):
                    raise CutoverError(
                        f'snapshot source {entry.name} modified '
                        'during preflight hash'
                    )
                if hasher.hexdigest() != entry.sha256:
                    raise CutoverError(
                        f'snapshot source {entry.name} sha256 mismatch'
                    )
            finally:
                os.close(sfd)

        # Repeat mutation exclusion and quiescence immediately before Phase 2.
        assert_mutation_exclusion(identity)
        assert_services_quiescent(
            identity,
            allow_non_linux=allow_non_linux,
            skip_systemd_check=allow_non_linux,
        )

        # Phase 2: Descriptor-relative unlink and restore.
        for service in identity.services:
            svc_id = service.service_id
            dest_dfd = dest_dir_fds[svc_id]
            src_dfd = src_dir_fds[svc_id]

            # Unlink current destination artifacts.
            for name in os.listdir(dest_dfd):
                if name in AUTHORITATIVE_STATE_NAMES:
                    os.unlink(name, dir_fd=dest_dfd)

            # Copy snapshot artifacts.
            for entry in snapshot.entries:
                if entry.service_id != svc_id or not entry.present:
                    continue
                name = entry.name
                sfd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=src_dfd)
                try:
                    dfd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                        mode=0o600,
                        dir_fd=dest_dfd,
                    )
                    try:
                        if expected_uid != 0:
                            with contextlib.suppress(OSError):
                                os.fchown(dfd, expected_uid, -1)

                        hasher = hashlib.sha256()
                        while chunk := os.read(sfd, 65536):
                            os.write(dfd, chunk)
                            hasher.update(chunk)
                        os.fsync(dfd)
                        if hasher.hexdigest() != entry.sha256:
                            raise CutoverError(
                                f'restored file {name} sha256 mismatch'
                            )
                    finally:
                        os.close(dfd)
                finally:
                    os.close(sfd)
            os.fsync(dest_dfd)
    finally:
        for dfd in dest_dir_fds.values():
            with contextlib.suppress(OSError):
                os.close(dfd)
        for dfd in src_dir_fds.values():
            with contextlib.suppress(OSError):
                os.close(dfd)


# CLI interface


def _emit_json(stream: TextIO, payload: object) -> None:
    """Emit JSON formatted payload."""
    json.dump(payload, stream, sort_keys=True, indent=2)
    stream.write('\n')


def _parser() -> argparse.ArgumentParser:
    """Build cutover command parser."""
    parser = argparse.ArgumentParser(
        prog='google-mcp-cutover',
        description='Cross-service cutover safety management tool.',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    # identity preview
    id_p = subparsers.add_parser('identity')
    id_sub = id_p.add_subparsers(dest='subcommand', required=True)
    id_prev = id_sub.add_parser('preview')
    id_prev.add_argument('--env-dir', type=Path, required=True)
    id_prev.add_argument('--uid', type=int, default=0)

    # reset preview / apply
    res_p = subparsers.add_parser('reset')
    res_sub = res_p.add_subparsers(dest='subcommand', required=True)
    res_prev = res_sub.add_parser('preview')
    res_prev.add_argument('--env-dir', type=Path, required=True)
    res_prev.add_argument('--state-root', type=Path, required=True)
    res_prev.add_argument('--uid', type=int, default=0)
    res_prev.add_argument('--env-uid', type=int, default=0)

    res_app = res_sub.add_parser('apply')
    res_app.add_argument('--identity-manifest', type=Path, required=True)
    res_app.add_argument('--manifest', type=Path, required=True)
    res_app.add_argument('--confirm-sha256', type=str, required=True)
    res_app.add_argument('--allow-non-linux', action='store_true')
    res_app.add_argument('--uid', type=int, default=0)
    res_app.add_argument('--env-uid', type=int, default=0)

    # maintenance attest
    m_p = subparsers.add_parser('maintenance')
    m_sub = m_p.add_subparsers(dest='subcommand', required=True)
    m_att = m_sub.add_parser('attest')
    m_att.add_argument('--identity-manifest', type=Path, required=True)
    m_att.add_argument('--output', type=Path, required=True)
    m_att.add_argument('--nginx-master-pid', type=int, required=True)
    m_att.add_argument('--nginx-config-digest', type=str, required=True)
    m_att.add_argument('--maintenance-include-target', type=str, required=True)
    m_att.add_argument('--worker-generation', type=int, required=True)
    m_att.add_argument('--uid', type=int, default=0)
    m_att.add_argument('--env-uid', type=int, default=0)

    # snapshot create / verify / restore
    snap_p = subparsers.add_parser('snapshot')
    snap_sub = snap_p.add_subparsers(dest='subcommand', required=True)

    snap_cr = snap_sub.add_parser('create')
    snap_cr.add_argument('--identity-manifest', type=Path, required=True)
    snap_cr.add_argument('--reset-manifest', type=Path, required=True)
    snap_cr.add_argument('--maintenance-attestation', type=Path, required=True)
    snap_cr.add_argument('--destination', type=Path, required=True)
    snap_cr.add_argument('--allow-non-linux', action='store_true')
    snap_cr.add_argument('--uid', type=int, default=0)
    snap_cr.add_argument('--env-uid', type=int, default=0)

    snap_ver = snap_sub.add_parser('verify')
    snap_ver.add_argument('--manifest', type=Path, required=True)
    snap_ver.add_argument('--uid', type=int, default=0)

    snap_res = snap_sub.add_parser('restore')
    snap_res.add_argument('--identity-manifest', type=Path, required=True)
    snap_res.add_argument('--manifest', type=Path, required=True)
    snap_res.add_argument('--journal', type=Path, required=True)
    snap_res.add_argument(
        '--maintenance-attestation', type=Path, required=True
    )
    snap_res.add_argument('--state-root', type=Path, required=True)
    snap_res.add_argument('--confirm-sha256', type=str, required=True)
    snap_res.add_argument('--allow-non-linux', action='store_true')
    snap_res.add_argument('--uid', type=int, default=0)
    snap_res.add_argument('--env-uid', type=int, default=0)

    # journal create / mark-gate-opened
    j_p = subparsers.add_parser('journal')
    j_sub = j_p.add_subparsers(dest='subcommand', required=True)

    j_cr = j_sub.add_parser('create')
    j_cr.add_argument('--identity-manifest', type=Path, required=True)
    j_cr.add_argument('--reset-manifest', type=Path, required=True)
    j_cr.add_argument('--snapshot-manifest', type=Path, required=True)
    j_cr.add_argument('--maintenance-attestation', type=Path, required=True)
    j_cr.add_argument('--output', type=Path, required=True)
    j_cr.add_argument('--uid', type=int, default=0)
    j_cr.add_argument('--env-uid', type=int, default=0)

    j_mark = j_sub.add_parser('mark-gate-opened')
    j_mark.add_argument('--journal', type=Path, required=True)
    j_mark.add_argument('--confirm-sha256', type=str, required=True)
    j_mark.add_argument('--uid', type=int, default=0)

    return parser


def _handle_identity_preview(args: argparse.Namespace, out: TextIO) -> int:
    """Handle identity preview command."""
    manifest = build_identity_manifest(
        args.env_dir,
        expected_uid=args.uid,
    )
    payload = {
        'env_dir': str(manifest.env_dir),
        'services': [
            {
                'service_id': s.service_id,
                'issuer': s.issuer,
                'resource': s.resource,
                'mcp_path': s.mcp_path,
                'port': s.port,
                'state_path': str(s.state_path),
                'google_token_path': str(s.google_token_path),
            }
            for s in manifest.services
        ],
        'digest': manifest.digest,
    }
    _emit_json(out, payload)
    return 0


def _handle_reset_preview(args: argparse.Namespace, out: TextIO) -> int:
    """Handle reset preview command."""
    env_uid = getattr(args, 'env_uid', args.uid)
    id_manifest = build_identity_manifest(args.env_dir, expected_uid=env_uid)
    reset_manifest = build_reset_manifest(
        id_manifest, args.state_root, expected_uid=args.uid
    )
    payload = {
        'state_root': str(reset_manifest.state_root),
        'identity_digest': reset_manifest.identity_digest,
        'directories': [
            {
                'service_id': d.service_id,
                'path': str(d.path),
                'device': d.device,
                'inode': d.inode,
                'owner': d.owner,
                'mode': d.mode,
            }
            for d in reset_manifest.directories
        ],
        'entries': [
            {
                'path': str(e.path),
                'present': e.present,
                'device': e.device,
                'inode': e.inode,
                'owner': e.owner,
                'mode': e.mode,
                'links': e.links,
                'size': e.size,
                'sha256': e.sha256,
            }
            for e in reset_manifest.entries
        ],
        'digest': reset_manifest.digest,
    }
    _emit_json(out, payload)
    return 0


def _handle_reset_apply(args: argparse.Namespace, out: TextIO) -> int:
    """Handle reset apply command."""
    env_uid = getattr(args, 'env_uid', 0)
    id_data = _load_secure_json(args.identity_manifest, expected_uid=env_uid)
    res_data = _load_secure_json(args.manifest, expected_uid=args.uid)
    if res_data['digest'] != args.confirm_sha256:
        raise CutoverError('confirm sha256 mismatch with manifest')

    services = tuple(
        ServiceIdentity(
            service_id=str(s['service_id']),
            issuer=str(s['issuer']),
            resource=str(s['resource']),
            mcp_path=str(s['mcp_path']),
            port=int(s['port']),
            state_path=Path(str(s['state_path'])),
            google_token_path=Path(str(s['google_token_path'])),
        )
        for s in id_data['services']
    )
    id_manifest = IdentityManifest(
        env_dir=Path(str(id_data['env_dir'])),
        services=services,
        digest=str(id_data['digest']),
    )

    directories = tuple(
        DirectoryIdentity(
            service_id=str(d['service_id']),
            path=Path(str(d['path'])),
            device=int(d['device']),
            inode=int(d['inode']),
            owner=int(d['owner']),
            mode=int(d['mode']),
        )
        for d in res_data['directories']
    )
    entries = tuple(
        ResetEntry(
            path=Path(str(e['path'])),
            present=bool(e['present']),
            device=int(e['device']) if e['device'] is not None else None,
            inode=int(e['inode']) if e['inode'] is not None else None,
            owner=int(e['owner']) if e['owner'] is not None else None,
            mode=int(e['mode']) if e['mode'] is not None else None,
            links=int(e['links']) if e['links'] is not None else None,
            size=int(e['size']) if e['size'] is not None else None,
            sha256=str(e['sha256']) if e['sha256'] is not None else None,
        )
        for e in res_data['entries']
    )
    res_manifest = ResetManifest(
        state_root=Path(str(res_data['state_root'])),
        identity_digest=str(res_data['identity_digest']),
        directories=directories,
        entries=entries,
        digest=str(res_data['digest']),
    )

    apply_reset_manifest(
        id_manifest,
        res_manifest,
        allow_non_linux=args.allow_non_linux,
        expected_env_uid=env_uid,
    )
    _emit_json(out, {'status': 'success', 'digest': res_data['digest']})
    return 0


def _handle_maintenance_attest(args: argparse.Namespace, out: TextIO) -> int:
    """Handle maintenance attest command."""
    env_uid = getattr(args, 'env_uid', 0)
    id_data = _load_secure_json(args.identity_manifest, expected_uid=env_uid)
    services = tuple(
        ServiceIdentity(
            service_id=str(s['service_id']),
            issuer=str(s['issuer']),
            resource=str(s['resource']),
            mcp_path=str(s['mcp_path']),
            port=int(s['port']),
            state_path=Path(str(s['state_path'])),
            google_token_path=Path(str(s['google_token_path'])),
        )
        for s in id_data['services']
    )
    id_manifest = IdentityManifest(
        env_dir=Path(str(id_data['env_dir'])),
        services=services,
        digest=str(id_data['digest']),
    )
    attestation = create_maintenance_attestation(
        identity=id_manifest,
        path=args.output,
        nginx_master_pid=args.nginx_master_pid,
        nginx_config_digest=args.nginx_config_digest,
        maintenance_include_target=args.maintenance_include_target,
        worker_generation=args.worker_generation,
    )
    _emit_json(out, asdict(attestation))
    return 0


def _handle_snapshot_create(args: argparse.Namespace, out: TextIO) -> int:
    """Handle snapshot create command."""
    env_uid = getattr(args, 'env_uid', 0)
    id_data = _load_secure_json(args.identity_manifest, expected_uid=env_uid)
    res_data = _load_secure_json(args.reset_manifest, expected_uid=args.uid)
    att_data = _load_secure_json(
        args.maintenance_attestation, expected_uid=args.uid
    )

    services = tuple(
        ServiceIdentity(
            service_id=str(s['service_id']),
            issuer=str(s['issuer']),
            resource=str(s['resource']),
            mcp_path=str(s['mcp_path']),
            port=int(s['port']),
            state_path=Path(str(s['state_path'])),
            google_token_path=Path(str(s['google_token_path'])),
        )
        for s in id_data['services']
    )
    id_manifest = IdentityManifest(
        env_dir=Path(str(id_data['env_dir'])),
        services=services,
        digest=str(id_data['digest']),
    )

    directories = tuple(
        DirectoryIdentity(
            service_id=str(d['service_id']),
            path=Path(str(d['path'])),
            device=int(d['device']),
            inode=int(d['inode']),
            owner=int(d['owner']),
            mode=int(d['mode']),
        )
        for d in res_data['directories']
    )
    entries = tuple(
        ResetEntry(
            path=Path(str(e['path'])),
            present=bool(e['present']),
            device=int(e['device']) if e['device'] is not None else None,
            inode=int(e['inode']) if e['inode'] is not None else None,
            owner=int(e['owner']) if e['owner'] is not None else None,
            mode=int(e['mode']) if e['mode'] is not None else None,
            links=int(e['links']) if e['links'] is not None else None,
            size=int(e['size']) if e['size'] is not None else None,
            sha256=str(e['sha256']) if e['sha256'] is not None else None,
        )
        for e in res_data['entries']
    )
    res_manifest = ResetManifest(
        state_root=Path(str(res_data['state_root'])),
        identity_digest=str(res_data['identity_digest']),
        directories=directories,
        entries=entries,
        digest=str(res_data['digest']),
    )

    attestation = MaintenanceAttestation(
        identity_digest=str(att_data['identity_digest']),
        nginx_master_pid=int(att_data['nginx_master_pid']),
        nginx_config_digest=str(att_data['nginx_config_digest']),
        maintenance_include_target=str(att_data['maintenance_include_target']),
        worker_generation=int(att_data['worker_generation']),
        digest=str(att_data['digest']),
    )

    verify_maintenance_attestation(id_manifest, attestation)

    snapshot = create_offline_snapshot(
        identity=id_manifest,
        reset=res_manifest,
        destination=args.destination,
        expected_uid=args.uid,
        expected_env_uid=env_uid,
        allow_non_linux=args.allow_non_linux,
    )
    payload = {
        'destination': str(snapshot.destination),
        'identity_digest': snapshot.identity_digest,
        'reset_digest': snapshot.reset_digest,
        'entries': [
            {
                'service_id': e.service_id,
                'name': e.name,
                'path': str(e.path),
                'present': e.present,
                'size': e.size,
                'sha256': e.sha256,
            }
            for e in snapshot.entries
        ],
        'digest': snapshot.digest,
    }
    _emit_json(out, payload)
    return 0


def _handle_snapshot_verify(args: argparse.Namespace, out: TextIO) -> int:
    """Handle snapshot verify command."""
    snap_data = _load_secure_json(args.manifest, expected_uid=args.uid)
    entries_snap = tuple(
        SnapshotEntry(
            service_id=str(e['service_id']),
            name=str(e['name']),
            path=Path(str(e['path'])),
            present=bool(e['present']),
            size=int(e['size']) if e['size'] is not None else None,
            sha256=str(e['sha256']) if e['sha256'] is not None else None,
        )
        for e in snap_data['entries']
    )
    snap_manifest = SnapshotManifest(
        destination=Path(str(snap_data['destination'])),
        identity_digest=str(snap_data['identity_digest']),
        reset_digest=str(snap_data['reset_digest']),
        entries=entries_snap,
        digest=str(snap_data['digest']),
    )
    verify_offline_snapshot(snap_manifest, expected_uid=args.uid)
    _emit_json(out, {'status': 'verified', 'digest': snap_manifest.digest})
    return 0


def _handle_journal_create(args: argparse.Namespace, out: TextIO) -> int:
    """Handle journal create command."""
    env_uid = getattr(args, 'env_uid', 0)
    id_data = _load_secure_json(args.identity_manifest, expected_uid=env_uid)
    res_data = _load_secure_json(args.reset_manifest, expected_uid=args.uid)
    snap_data = _load_secure_json(
        args.snapshot_manifest, expected_uid=args.uid
    )
    att_data = _load_secure_json(
        args.maintenance_attestation, expected_uid=args.uid
    )

    services = tuple(
        ServiceIdentity(
            service_id=str(s['service_id']),
            issuer=str(s['issuer']),
            resource=str(s['resource']),
            mcp_path=str(s['mcp_path']),
            port=int(s['port']),
            state_path=Path(str(s['state_path'])),
            google_token_path=Path(str(s['google_token_path'])),
        )
        for s in id_data['services']
    )
    id_manifest = IdentityManifest(
        env_dir=Path(str(id_data['env_dir'])),
        services=services,
        digest=str(id_data['digest']),
    )

    directories = tuple(
        DirectoryIdentity(
            service_id=str(d['service_id']),
            path=Path(str(d['path'])),
            device=int(d['device']),
            inode=int(d['inode']),
            owner=int(d['owner']),
            mode=int(d['mode']),
        )
        for d in res_data['directories']
    )
    entries_res = tuple(
        ResetEntry(
            path=Path(str(e['path'])),
            present=bool(e['present']),
            device=int(e['device']) if e['device'] is not None else None,
            inode=int(e['inode']) if e['inode'] is not None else None,
            owner=int(e['owner']) if e['owner'] is not None else None,
            mode=int(e['mode']) if e['mode'] is not None else None,
            links=int(e['links']) if e['links'] is not None else None,
            size=int(e['size']) if e['size'] is not None else None,
            sha256=str(e['sha256']) if e['sha256'] is not None else None,
        )
        for e in res_data['entries']
    )
    res_manifest = ResetManifest(
        state_root=Path(str(res_data['state_root'])),
        identity_digest=str(res_data['identity_digest']),
        directories=directories,
        entries=entries_res,
        digest=str(res_data['digest']),
    )

    entries_snap = tuple(
        SnapshotEntry(
            service_id=str(e['service_id']),
            name=str(e['name']),
            path=Path(str(e['path'])),
            present=bool(e['present']),
            size=int(e['size']) if e['size'] is not None else None,
            sha256=str(e['sha256']) if e['sha256'] is not None else None,
        )
        for e in snap_data['entries']
    )
    snap_manifest = SnapshotManifest(
        destination=Path(str(snap_data['destination'])),
        identity_digest=str(snap_data['identity_digest']),
        reset_digest=str(snap_data['reset_digest']),
        entries=entries_snap,
        digest=str(snap_data['digest']),
    )

    attestation = MaintenanceAttestation(
        identity_digest=str(att_data['identity_digest']),
        nginx_master_pid=int(att_data['nginx_master_pid']),
        nginx_config_digest=str(att_data['nginx_config_digest']),
        maintenance_include_target=str(att_data['maintenance_include_target']),
        worker_generation=int(att_data['worker_generation']),
        digest=str(att_data['digest']),
    )

    journal = create_cutover_journal(
        identity=id_manifest,
        reset=res_manifest,
        snapshot=snap_manifest,
        maintenance=attestation,
        path=args.output,
    )
    payload = asdict(journal)
    payload['path'] = str(journal.path)
    _emit_json(out, payload)
    return 0


def _handle_journal_mark_gate_opened(
    args: argparse.Namespace, out: TextIO
) -> int:
    """Handle mark gate command."""
    j_data = _load_secure_json(args.journal, expected_uid=args.uid)
    if j_data['digest'] != args.confirm_sha256:
        raise CutoverError('confirm sha256 mismatch with journal')

    journal = CutoverJournal(
        path=Path(str(j_data['path'])),
        state=str(j_data['state']),
        identity_digest=str(j_data['identity_digest']),
        reset_digest=str(j_data['reset_digest']),
        snapshot_digest=str(j_data['snapshot_digest']),
        maintenance_digest=str(j_data['maintenance_digest']),
        digest=str(j_data['digest']),
    )
    opened = mark_gate_opened(journal)
    payload = asdict(opened)
    payload['path'] = str(opened.path)
    _emit_json(out, payload)
    return 0


def _handle_snapshot_restore(args: argparse.Namespace, out: TextIO) -> int:
    """Handle snapshot restore command."""
    env_uid = getattr(args, 'env_uid', 0)
    id_data = _load_secure_json(args.identity_manifest, expected_uid=env_uid)
    snap_data = _load_secure_json(args.manifest, expected_uid=args.uid)
    j_data = _load_secure_json(args.journal, expected_uid=args.uid)
    att_data = _load_secure_json(
        args.maintenance_attestation, expected_uid=args.uid
    )

    if snap_data['digest'] != args.confirm_sha256:
        raise CutoverError('confirm sha256 mismatch with snapshot')

    services = tuple(
        ServiceIdentity(
            service_id=str(s['service_id']),
            issuer=str(s['issuer']),
            resource=str(s['resource']),
            mcp_path=str(s['mcp_path']),
            port=int(s['port']),
            state_path=Path(str(s['state_path'])),
            google_token_path=Path(str(s['google_token_path'])),
        )
        for s in id_data['services']
    )
    id_manifest = IdentityManifest(
        env_dir=Path(str(id_data['env_dir'])),
        services=services,
        digest=str(id_data['digest']),
    )

    entries_snap = tuple(
        SnapshotEntry(
            service_id=str(e['service_id']),
            name=str(e['name']),
            path=Path(str(e['path'])),
            present=bool(e['present']),
            size=int(e['size']) if e['size'] is not None else None,
            sha256=str(e['sha256']) if e['sha256'] is not None else None,
        )
        for e in snap_data['entries']
    )
    snap_manifest = SnapshotManifest(
        destination=Path(str(snap_data['destination'])),
        identity_digest=str(snap_data['identity_digest']),
        reset_digest=str(snap_data['reset_digest']),
        entries=entries_snap,
        digest=str(snap_data['digest']),
    )

    journal = CutoverJournal(
        path=Path(str(j_data['path'])),
        state=str(j_data['state']),
        identity_digest=str(j_data['identity_digest']),
        reset_digest=str(j_data['reset_digest']),
        snapshot_digest=str(j_data['snapshot_digest']),
        maintenance_digest=str(j_data['maintenance_digest']),
        digest=str(j_data['digest']),
    )

    attestation = MaintenanceAttestation(
        identity_digest=str(att_data['identity_digest']),
        nginx_master_pid=int(att_data['nginx_master_pid']),
        nginx_config_digest=str(att_data['nginx_config_digest']),
        maintenance_include_target=str(att_data['maintenance_include_target']),
        worker_generation=int(att_data['worker_generation']),
        digest=str(att_data['digest']),
    )

    restore_offline_snapshot(
        identity=id_manifest,
        snapshot=snap_manifest,
        journal=journal,
        maintenance=attestation,
        state_root=args.state_root,
        expected_uid=args.uid,
        expected_env_uid=env_uid,
        allow_non_linux=args.allow_non_linux,
    )
    _emit_json(out, {'status': 'restored', 'digest': snap_manifest.digest})
    return 0


def main(
    argv: list[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Execute cutover tool operation."""
    out = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    handlers = {
        ('identity', 'preview'): _handle_identity_preview,
        ('reset', 'preview'): _handle_reset_preview,
        ('reset', 'apply'): _handle_reset_apply,
        ('maintenance', 'attest'): _handle_maintenance_attest,
        ('snapshot', 'create'): _handle_snapshot_create,
        ('snapshot', 'verify'): _handle_snapshot_verify,
        ('snapshot', 'restore'): _handle_snapshot_restore,
        ('journal', 'create'): _handle_journal_create,
        ('journal', 'mark-gate-opened'): _handle_journal_mark_gate_opened,
    }

    try:
        cmd_key = (str(args.command), str(getattr(args, 'subcommand', '')))
        handler = handlers.get(cmd_key)
        if handler is None:
            raise CutoverError(f'unrecognized cutover command: {cmd_key}')
        return handler(args, out)
    except CutoverError as exc:
        error_stream.write(f'cutover error: {exc}\n')
        return 1
    except Exception as exc:
        error_stream.write(f'unexpected cutover failure: {exc}\n')
        return 1

    return 0


def _entrypoint() -> None:
    """Run cutover entrypoint."""
    raise SystemExit(main())


if __name__ == '__main__':
    _entrypoint()
