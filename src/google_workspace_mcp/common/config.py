"""Shared service configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
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


def _required_path(env: Mapping[str, str], key: str, default: Path) -> Path:
    """Resolve a required path."""
    value = env.get(key)
    if value is None:
        return default
    return Path(value.strip()).expanduser()


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


# === Service configuration model ===


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Store service runtime settings."""

    service_id: str
    public_url: str
    mcp_path: str
    host: str
    port: int
    oauth_state_path: Path
    google_token_path: Path
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
        host = env.get(f'{prefix}_MCP_HOST', '127.0.0.1').strip()
        public_url = (
            env.get(f'{prefix}_MCP_PUBLIC_URL', f'http://127.0.0.1:{port}')
            .strip()
            .rstrip('/')
        )
        mcp_path = env.get(f'{prefix}_MCP_PATH', f'/{service}/mcp').strip()
        state_dir = (_STATE_ROOT / service).expanduser()
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
            oauth_state_path=_required_path(
                env,
                f'{prefix}_OAUTH_STATE_PATH',
                state_dir / 'oauth_state.sqlite3',
            ),
            google_token_path=_required_path(
                env,
                f'{prefix}_GOOGLE_TOKEN_PATH',
                state_dir / 'google_token.json',
            ),
            allowed_hosts=_csv(env.get(f'{prefix}_MCP_ALLOWED_HOSTS')),
            forwarded_allow_ips=(
                _csv(env.get(f'{prefix}_MCP_FORWARDED_ALLOW_IPS'))
                if f'{prefix}_MCP_FORWARDED_ALLOW_IPS' in env
                else ('127.0.0.1',)
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
