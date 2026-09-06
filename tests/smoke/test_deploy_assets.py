"""Check production deploy assets."""

from pathlib import Path

import pytest

from google_workspace_mcp.common.config import ServiceConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = PROJECT_ROOT / 'deploy'
ENV_ROOT = DEPLOY_ROOT / 'env'
SYSTEMD_UNIT = DEPLOY_ROOT / 'google-mcp@.service'
WARMUP_UNIT = DEPLOY_ROOT / 'google-mcp-warmup.service'
WARMUP_TIMER = DEPLOY_ROOT / 'google-mcp-warmup.timer'
SERVICES = {
    'gmail': 8431,
    'calendar': 8432,
    'drive': 8433,
    'sheets': 8434,
    'docs': 8435,
}
ENV_SUFFIXES = {
    'MCP_PUBLIC_URL',
    'MCP_HOST',
    'MCP_PORT',
    'MCP_PATH',
    'MCP_ALLOWED_HOSTS',
    'MCP_FORWARDED_ALLOW_IPS',
    'OAUTH_STATE_PATH',
    'GOOGLE_TOKEN_PATH',
    'AUDIT_LOG_PATH',
    'MCP_DOWNLOAD_PATH',
    'OAUTH_LOGIN_USERNAME',
    'OAUTH_LOGIN_PASSWORD',
}


def _read_env(path: Path) -> dict[str, str]:
    """Read environment example values."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        assert separator == '=', f'invalid environment line: {raw_line!r}'
        assert key not in values, f'duplicate environment key: {key}'
        values[key] = value
    return values


def test_environment_examples_match_service_contract() -> None:
    all_paths: set[str] = set()
    known_prefixes = {service.upper() for service in SERVICES}

    for service, port in SERVICES.items():
        prefix = service.upper()
        values = _read_env(ENV_ROOT / f'{service}.env.example')
        expected_keys = {f'{prefix}_{suffix}' for suffix in ENV_SUFFIXES}

        assert set(values) == expected_keys
        assert values[f'{prefix}_MCP_PUBLIC_URL'] == (
            f'https://mcp.hawkxdev.dev/{service}'
        )
        assert values[f'{prefix}_MCP_HOST'] == '127.0.0.1'
        assert values[f'{prefix}_MCP_PORT'] == str(port)
        assert values[f'{prefix}_MCP_PATH'] == f'/{service}/mcp'
        assert values[f'{prefix}_MCP_ALLOWED_HOSTS'] == 'mcp.hawkxdev.dev'
        assert values[f'{prefix}_MCP_FORWARDED_ALLOW_IPS'] == '127.0.0.1'
        assert values[f'{prefix}_OAUTH_LOGIN_USERNAME'] == ''
        assert values[f'{prefix}_OAUTH_LOGIN_PASSWORD'] == ''

        state_root = f'/var/lib/google-workspace-mcp/{service}'
        expected_paths = {
            f'{state_root}/oauth_state.sqlite3',
            f'{state_root}/google_token.json',
            f'{state_root}/audit.jsonl',
            f'{state_root}/downloads',
        }
        actual_paths = {
            values[f'{prefix}_OAUTH_STATE_PATH'],
            values[f'{prefix}_GOOGLE_TOKEN_PATH'],
            values[f'{prefix}_AUDIT_LOG_PATH'],
            values[f'{prefix}_MCP_DOWNLOAD_PATH'],
        }
        assert actual_paths == expected_paths
        assert all_paths.isdisjoint(actual_paths)
        all_paths.update(actual_paths)

        for key in values:
            key_prefix = key.split('_', 1)[0]
            assert key_prefix == prefix
            assert key_prefix in known_prefixes

    assert len(all_paths) == 20


def test_environment_examples_build_runtime_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for service, port in SERVICES.items():
        prefix = service.upper()
        values = _read_env(ENV_ROOT / f'{service}.env.example')
        values[f'{prefix}_OAUTH_LOGIN_USERNAME'] = 'operator'
        values[f'{prefix}_OAUTH_LOGIN_PASSWORD'] = 'test-password'
        for key, value in values.items():
            monkeypatch.setenv(key, value)

        config = ServiceConfig.from_env(service)

        assert config.service_id == service
        assert config.public_url == f'https://mcp.hawkxdev.dev/{service}'
        assert config.host == '127.0.0.1'
        assert config.port == port
        assert config.mcp_path == f'/{service}/mcp'
        assert config.allowed_hosts == ('mcp.hawkxdev.dev',)
        assert config.forwarded_allow_ips == ('127.0.0.1',)
        state_root = Path(f'/var/lib/google-workspace-mcp/{service}')
        assert config.oauth_state_path == state_root / 'oauth_state.sqlite3'
        assert config.google_token_path == state_root / 'google_token.json'
        assert config.audit_log_path == state_root / 'audit.jsonl'
        assert config.download_path == state_root / 'downloads'


def test_environment_directory_contains_only_service_examples() -> None:
    assert {path.name for path in ENV_ROOT.iterdir()} == {
        f'{service}.env.example' for service in SERVICES
    }


def test_systemd_template_runs_isolated_service_instances() -> None:
    unit = SYSTEMD_UNIT.read_text(encoding='utf-8')

    for directive in (
        'After=network-online.target',
        'Wants=network-online.target',
        'Type=simple',
        'User=googlemcp',
        'Group=googlemcp',
        'WorkingDirectory=/opt/google-workspace-mcp/app',
        'Environment=HOME=/var/lib/google-workspace-mcp',
        'EnvironmentFile=/etc/google-mcp/%i.env',
        'ExecStart=/opt/google-workspace-mcp/app/.venv/bin/google-mcp-%i',
        'Restart=on-failure',
        'RestartSec=5',
        'NoNewPrivileges=true',
        'PrivateTmp=true',
        'ProtectSystem=full',
        'ProtectHome=true',
        'ProtectKernelTunables=true',
        'ProtectControlGroups=true',
        'RestrictSUIDSGID=true',
        'WantedBy=multi-user.target',
    ):
        assert directive in unit

    for donor_policy in (
        'ReadWritePaths=',
        'KillSignal=',
        'TimeoutStopSec=',
        '8420',
        'vault-mcp',
        'mcp.hawkxdev.dev',
    ):
        assert donor_policy not in unit


def _directives(unit: Path) -> list[tuple[str, str]]:
    """Read unit directives as ordered key value pairs."""
    pairs: list[tuple[str, str]] = []
    for raw_line in unit.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or line.startswith('['):
            continue
        key, separator, value = line.partition('=')
        assert separator == '=', f'invalid unit line: {raw_line!r}'
        pairs.append((key.strip(), value.strip()))
    return pairs


def test_warmup_unit_runs_once_as_the_service_account() -> None:
    directives = dict(_directives(WARMUP_UNIT))

    assert directives['Type'] == 'oneshot'
    assert directives['User'] == 'googlemcp'
    assert directives['Group'] == 'googlemcp'
    # A warm-up that failed must not be retried into the services.
    assert directives['Restart'] == 'no'
    assert directives['ExecStart'] == (
        '/opt/google-workspace-mcp/app/.venv/bin/google-mcp-warmup'
    )


def test_warmup_unit_loads_every_service_environment() -> None:
    loaded = [
        value
        for key, value in _directives(WARMUP_UNIT)
        if key == 'EnvironmentFile'
    ]

    assert loaded == [f'/etc/google-mcp/{service}.env' for service in SERVICES]


def test_warmup_timer_uses_a_schedule_oneshot_supports() -> None:
    directives = dict(_directives(WARMUP_TIMER))

    # OnUnitActiveSec never fires for Type=oneshot: such a unit does not
    # reach the active state. This pins the property, not the wording.
    assert 'OnUnitActiveSec' not in directives
    assert directives['OnCalendar'] == 'monthly'
    # Persistent is calendar-only and required here: a skipped month
    # must be caught up, because the provider counts inactivity in
    # months.
    assert directives['Persistent'] == 'true'
    assert directives['Unit'] == WARMUP_UNIT.name
    assert directives['WantedBy'] == 'timers.target'


def test_warmup_interval_keeps_margin_against_the_provider_threshold() -> None:
    directives = dict(_directives(WARMUP_TIMER))
    calendar = directives['OnCalendar']
    monthly_forms = {'monthly', '*-*-01 00:00:00'}

    # The provider retires an unused client or refresh token after six
    # months. Anything coarser than monthly spends the margin.
    assert calendar in monthly_forms
