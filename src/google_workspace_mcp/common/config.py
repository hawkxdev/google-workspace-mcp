"""Shared service configuration."""

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_STATE_ROOT = Path('~/.local/share/google-workspace-mcp')

# === Environment parsing helpers ===


def _default_port(service: str) -> int:
    """Return the service port."""
    match service:
        case 'gmail':
            return 8431
        case 'calendar':
            return 8432
        case 'drive':
            return 8433
        case 'sheets':
            return 8434
        case 'docs':
            return 8435
        case _:
            raise ValueError(f'unsupported service: {service}')


def _csv(value: str | None) -> tuple[str, ...]:
    """Parse comma separated values."""
    if value is None:
        return ()
    result: list[str] = []
    for item in value.split(','):
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return tuple(result)


def _path(value: str | None) -> Path | None:
    """Parse an optional path."""
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def _required_string(env: Mapping[str, str], key: str, default: str) -> str:
    """Resolve a required value."""
    value = env.get(key, default).strip()
    if not value:
        raise ValueError(f'{key} must not be empty')
    return value


def _required_path(env: Mapping[str, str], key: str, default: Path) -> Path:
    """Resolve a required path."""
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise ValueError(f'{key} must not be empty')
    return Path(value).expanduser()


def _integer(
    env: Mapping[str, str], key: str, default: int, low: int, high: int
) -> int:
    """Parse a bounded integer."""
    value = env.get(key)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{key} must be an integer') from exc
    if not low <= parsed <= high:
        raise ValueError(f'{key} must be between {low} and {high}')
    return parsed


def _validate_mcp_path(path: str) -> None:
    """Validate MCP streamable path."""
    if path == '/':
        return
    if not path.startswith('/'):
        raise ValueError(
            f"MCP path must be an absolute path starting with '/': {path!r}"
        )
    if path.endswith('/'):
        raise ValueError(
            f'MCP path must not end with a trailing slash: {path!r}'
        )
    if '?' in path or '#' in path or '//' in path:
        raise ValueError(
            'MCP path must be a clean path with no query string, fragment, '
            f'or empty segments: {path!r}'
        )
    if '%' in path or any(c.isspace() or ord(c) < 0x20 for c in path):
        raise ValueError(
            'MCP path must not contain percent-encoding, whitespace, or '
            f'control characters: {path!r}'
        )
    if any(seg in ('.', '..') for seg in path.strip('/').split('/')):
        raise ValueError(
            f"MCP path must not contain '.' or '..' path segments: {path!r}"
        )
    reserved_prefixes = ('/oauth', '/.well-known')
    collides = path in ('/health', '/ready') or any(
        path == prefix or path.startswith(prefix + '/')
        for prefix in reserved_prefixes
    )
    if collides:
        raise ValueError(
            f'MCP path {path!r} collides with an authentication-exempt or '
            'system route'
        )


def _is_unbounded_proxy_entry(entry: str) -> bool:
    """Detect unbounded proxy entry."""
    if '*' in entry:
        return True
    candidate = entry.strip()
    if not candidate:
        return False
    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        return False
    return network.prefixlen == 0 or network.network_address.is_unspecified


def validate_forwarded_allow_ips(
    ips: tuple[str, ...], prefix: str
) -> tuple[str, ...]:
    """Validate forwarded IP list."""
    if any(_is_unbounded_proxy_entry(ip) for ip in ips):
        raise ValueError(
            f'{prefix}_MCP_FORWARDED_ALLOW_IPS must not contain a wildcard '
            'or an unbounded network'
        )
    return ips


# === Service configuration model ===


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Store service runtime settings."""

    service_id: str
    public_url: str
    mcp_path: str
    host: str
    port: int
    download_path: Path
    oauth_state_path: Path
    google_token_path: Path
    audit_log_path: Path
    oauth_login_username: str
    oauth_login_password: str = field(repr=False)
    allowed_hosts: tuple[str, ...]
    forwarded_allow_ips: tuple[str, ...]
    legacy_clients_path: Path | None
    approved_legacy_client_ids: frozenset[str]
    access_token_ttl_seconds: int
    refresh_token_ttl_seconds: int

    @classmethod
    def from_env(cls, service: str) -> ServiceConfig:
        """Build settings from environment."""
        prefix = service.upper()
        env = os.environ
        # 1. Parse core settings
        port = _integer(
            env, f'{prefix}_MCP_PORT', _default_port(service), 1, 65535
        )
        host_key = f'{prefix}_MCP_HOST'
        public_url_key = f'{prefix}_MCP_PUBLIC_URL'
        mcp_path_key = f'{prefix}_MCP_PATH'
        host = _required_string(env, host_key, '127.0.0.1')
        public_url_val = env.get(public_url_key)
        if public_url_val is None or not public_url_val.strip():
            raise ValueError(
                f'{public_url_key} must be set to the public HTTPS URL of '
                'this service'
            )
        public_url = public_url_val.strip()
        mcp_path = _required_string(env, mcp_path_key, f'/{service}/mcp')
        _validate_mcp_path(mcp_path)
        state_dir = (_STATE_ROOT / service).expanduser()
        download_key = f'{prefix}_MCP_DOWNLOAD_PATH'
        download_path = _required_path(
            env, download_key, state_dir / 'downloads'
        )
        state_key = f'{prefix}_OAUTH_STATE_PATH'
        token_key = f'{prefix}_GOOGLE_TOKEN_PATH'
        audit_key = f'{prefix}_AUDIT_LOG_PATH'
        state_path = _required_path(
            env, state_key, state_dir / 'oauth_state.sqlite3'
        )
        token_path = _required_path(
            env, token_key, state_dir / 'google_token.json'
        )
        audit_path = _required_path(env, audit_key, state_dir / 'audit.jsonl')
        if os.path.abspath(state_path) == os.path.abspath(token_path):
            raise ValueError(f'{state_key} and {token_key} must differ')
        if os.path.abspath(audit_path) == os.path.abspath(state_path):
            raise ValueError(f'{audit_key} and {state_key} must differ')
        if os.path.abspath(audit_path) == os.path.abspath(token_path):
            raise ValueError(f'{audit_key} and {token_key} must differ')
        username_key = f'{prefix}_OAUTH_LOGIN_USERNAME'
        password_key = f'{prefix}_OAUTH_LOGIN_PASSWORD'
        username_val = env.get(username_key)
        if username_val is None or not username_val.strip():
            raise ValueError(f'{username_key} must not be empty')
        password_val = env.get(password_key)
        if password_val is None or not password_val.strip():
            raise ValueError(f'{password_key} must not be empty')
        # 2. Parse OAuth settings
        approved = frozenset(
            _csv(env.get(f'{prefix}_OAUTH_APPROVED_LEGACY_CLIENT_IDS'))
        )
        # 3. Build service configuration
        return cls(
            service_id=service,
            public_url=public_url,
            mcp_path=mcp_path,
            host=host,
            port=port,
            download_path=download_path,
            oauth_state_path=state_path,
            google_token_path=token_path,
            audit_log_path=audit_path,
            oauth_login_username=username_val.strip(),
            oauth_login_password=password_val.strip(),
            allowed_hosts=_csv(env.get(f'{prefix}_MCP_ALLOWED_HOSTS')),
            forwarded_allow_ips=(
                validate_forwarded_allow_ips(
                    _csv(env.get(f'{prefix}_MCP_FORWARDED_ALLOW_IPS'))
                    if f'{prefix}_MCP_FORWARDED_ALLOW_IPS' in env
                    else ('127.0.0.1',),
                    prefix,
                )
            ),
            legacy_clients_path=_path(
                env.get(f'{prefix}_OAUTH_LEGACY_CLIENTS_PATH')
            ),
            approved_legacy_client_ids=approved,
            access_token_ttl_seconds=_integer(
                env,
                f'{prefix}_OAUTH_ACCESS_TOKEN_TTL_SECONDS',
                86400,
                1,
                2592000,
            ),
            refresh_token_ttl_seconds=_integer(
                env,
                f'{prefix}_OAUTH_REFRESH_TOKEN_TTL_SECONDS',
                2592000,
                1,
                7776000,
            ),
        )
