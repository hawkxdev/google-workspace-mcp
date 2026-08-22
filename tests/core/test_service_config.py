"""Test service configuration behavior."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from google_workspace_mcp.common.config import ServiceConfig

# Shared configuration setup

SERVICES_AND_PORTS = {
    'gmail': 8431,
    'calendar': 8432,
    'drive': 8433,
    'sheets': 8434,
    'docs': 8435,
}

CONFIG_ENV_SUFFIXES = (
    'MCP_PUBLIC_URL',
    'MCP_PATH',
    'MCP_HOST',
    'MCP_PORT',
    'OAUTH_STATE_PATH',
    'GOOGLE_TOKEN_PATH',
    'MCP_ALLOWED_HOSTS',
    'MCP_FORWARDED_ALLOW_IPS',
    'OAUTH_LEGACY_CLIENTS_PATH',
    'OAUTH_APPROVED_LEGACY_CLIENT_IDS',
    'OAUTH_ACCESS_TOKEN_TTL_SECONDS',
    'OAUTH_REFRESH_TOKEN_TTL_SECONDS',
)


@pytest.fixture(autouse=True)
def clear_service_config_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clear service configuration variables."""
    for service in SERVICES_AND_PORTS:
        for suffix in CONFIG_ENV_SUFFIXES:
            monkeypatch.delenv(f'{service.upper()}_{suffix}', raising=False)


# Valid configuration cases
class TestValidConfiguration:
    """Valid configuration cases."""

    def test_defaults_are_exact(self) -> None:
        config = ServiceConfig.from_env('gmail')
        state_dir = Path.home() / '.local/share/google-workspace-mcp/gmail'

        assert config.service_id == 'gmail'
        assert config.public_url == 'http://127.0.0.1:8431'
        assert config.mcp_path == '/gmail/mcp'
        assert config.host == '127.0.0.1'
        assert config.port == 8431
        assert config.oauth_state_path == state_dir / 'oauth_state.sqlite3'
        assert config.google_token_path == state_dir / 'google_token.json'
        assert config.allowed_hosts == ()
        assert config.forwarded_allow_ips == ('127.0.0.1',)
        assert config.legacy_clients_path is None
        assert config.approved_legacy_client_ids == frozenset()
        assert config.access_token_ttl_seconds == 86400
        assert config.refresh_token_ttl_seconds == 2592000

    @pytest.mark.parametrize('service, port', SERVICES_AND_PORTS.items())
    def test_all_services_use_their_declared_port(
        self, service: str, port: int
    ) -> None:
        config = ServiceConfig.from_env(service)
        assert config.service_id == service
        assert config.port == port
        assert config.public_url == f'http://127.0.0.1:{port}'
        assert config.mcp_path == f'/{service}/mcp'

    def test_all_environment_fields_override_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        values = {
            'GMAIL_MCP_PUBLIC_URL': 'https://mail.example.test',
            'GMAIL_MCP_PATH': '/custom/mcp',
            'GMAIL_MCP_HOST': '127.0.0.2',
            'GMAIL_MCP_PORT': '9001',
            'GMAIL_OAUTH_STATE_PATH': '/var/lib/mail/oauth.json',
            'GMAIL_GOOGLE_TOKEN_PATH': '/var/lib/mail/token.json',
            'GMAIL_MCP_ALLOWED_HOSTS': (
                ' mail.example.test, api.example.test,mail.example.test '
            ),
            'GMAIL_MCP_FORWARDED_ALLOW_IPS': ' 10.0.0.1, 127.0.0.1,10.0.0.1 ',
            'GMAIL_OAUTH_LEGACY_CLIENTS_PATH': '/var/lib/mail/legacy.json',
            'GMAIL_OAUTH_APPROVED_LEGACY_CLIENT_IDS': ' old-a, old-b,old-a ',
            'GMAIL_OAUTH_ACCESS_TOKEN_TTL_SECONDS': '123',
            'GMAIL_OAUTH_REFRESH_TOKEN_TTL_SECONDS': '456',
        }
        for name, value in values.items():
            monkeypatch.setenv(name, value)

        config = ServiceConfig.from_env('gmail')

        assert config.service_id == 'gmail'
        assert config.public_url == 'https://mail.example.test'
        assert config.mcp_path == '/custom/mcp'
        assert config.host == '127.0.0.2'
        assert config.port == 9001
        assert config.oauth_state_path == Path('/var/lib/mail/oauth.json')
        assert config.google_token_path == Path('/var/lib/mail/token.json')
        assert config.allowed_hosts == (
            'mail.example.test',
            'api.example.test',
        )
        assert config.forwarded_allow_ips == ('10.0.0.1', '127.0.0.1')
        assert config.legacy_clients_path == Path('/var/lib/mail/legacy.json')
        assert config.approved_legacy_client_ids == frozenset(
            {'old-a', 'old-b'}
        )
        assert config.access_token_ttl_seconds == 123
        assert config.refresh_token_ttl_seconds == 456

    def test_csv_values_trim_empty_and_deduplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv('DRIVE_MCP_ALLOWED_HOSTS', ' a, ,b,a,, b ')
        monkeypatch.setenv(
            'DRIVE_MCP_FORWARDED_ALLOW_IPS', ', 127.0.0.1,10.1.1.1,127.0.0.1,'
        )
        monkeypatch.setenv(
            'DRIVE_OAUTH_APPROVED_LEGACY_CLIENT_IDS', 'x,, x, y,'
        )

        config = ServiceConfig.from_env('drive')

        assert config.allowed_hosts == ('a', 'b')
        assert config.forwarded_allow_ips == ('127.0.0.1', '10.1.1.1')
        assert config.approved_legacy_client_ids == frozenset({'x', 'y'})

    def test_empty_forwarded_allow_ips_disables_trust(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv('GMAIL_MCP_FORWARDED_ALLOW_IPS', '')

        assert ServiceConfig.from_env('gmail').forwarded_allow_ips == ()

    def test_state_and_token_paths_are_distinct_per_service(self) -> None:
        configs = [
            ServiceConfig.from_env(service) for service in SERVICES_AND_PORTS
        ]
        assert len({config.oauth_state_path for config in configs}) == 5
        assert len({config.google_token_path for config in configs}) == 5

    @pytest.mark.parametrize(
        'state_suffix, token_suffix',
        [
            ('shared', 'shared'),
            ('nested/../shared', 'shared'),
        ],
    )
    def test_state_and_token_paths_must_differ(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        state_suffix: str,
        token_suffix: str,
    ) -> None:
        monkeypatch.setenv(
            'GMAIL_OAUTH_STATE_PATH', str(tmp_path / state_suffix)
        )
        monkeypatch.setenv(
            'GMAIL_GOOGLE_TOKEN_PATH', str(tmp_path / token_suffix)
        )

        with pytest.raises(
            ValueError,
            match='GMAIL_OAUTH_STATE_PATH.*GMAIL_GOOGLE_TOKEN_PATH',
        ):
            ServiceConfig.from_env('gmail')


# Invalid configuration cases
class TestInvalidConfiguration:
    """Invalid configuration cases."""

    def test_unsupported_service_is_rejected(self) -> None:
        with pytest.raises(ValueError, match='unsupported service'):
            ServiceConfig.from_env('photos')

    @pytest.mark.parametrize(
        'variable',
        [
            'MCP_HOST',
            'MCP_PUBLIC_URL',
            'MCP_PATH',
            'OAUTH_STATE_PATH',
            'GOOGLE_TOKEN_PATH',
        ],
    )
    @pytest.mark.parametrize('value', ['', '   '])
    def test_empty_required_values_are_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        variable: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(f'GMAIL_{variable}', value)

        with pytest.raises(ValueError, match=f'GMAIL_{variable}'):
            ServiceConfig.from_env('gmail')

    @pytest.mark.parametrize(
        'variable',
        [
            'MCP_PORT',
            'OAUTH_ACCESS_TOKEN_TTL_SECONDS',
            'OAUTH_REFRESH_TOKEN_TTL_SECONDS',
        ],
    )
    def test_non_integer_numeric_values_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.setenv(f'CALENDAR_{variable}', 'not-an-integer')
        with pytest.raises(ValueError, match=f'CALENDAR_{variable}'):
            ServiceConfig.from_env('calendar')

    @pytest.mark.parametrize(
        'variable',
        ['OAUTH_ACCESS_TOKEN_TTL_SECONDS', 'OAUTH_REFRESH_TOKEN_TTL_SECONDS'],
    )
    def test_empty_ttl_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.setenv(f'DOCS_{variable}', '')
        with pytest.raises(ValueError, match=f'DOCS_{variable}'):
            ServiceConfig.from_env('docs')


# Configuration boundary cases
class TestBoundaryConfiguration:
    """Boundary configuration cases."""

    @pytest.mark.parametrize(
        'env_suffix, attribute, minimum, maximum',
        [
            (
                'OAUTH_ACCESS_TOKEN_TTL_SECONDS',
                'access_token_ttl_seconds',
                1,
                2592000,
            ),
            (
                'OAUTH_REFRESH_TOKEN_TTL_SECONDS',
                'refresh_token_ttl_seconds',
                1,
                7776000,
            ),
        ],
    )
    def test_ttl_bounds_are_inclusive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_suffix: str,
        attribute: str,
        minimum: int,
        maximum: int,
    ) -> None:
        monkeypatch.setenv(f'SHEETS_{env_suffix}', str(minimum))
        assert getattr(ServiceConfig.from_env('sheets'), attribute) == minimum
        monkeypatch.setenv(f'SHEETS_{env_suffix}', str(maximum))
        assert getattr(ServiceConfig.from_env('sheets'), attribute) == maximum

    @pytest.mark.parametrize('value', [1, 65535])
    def test_port_bounds_are_inclusive(
        self, monkeypatch: pytest.MonkeyPatch, value: int
    ) -> None:
        monkeypatch.setenv('GMAIL_MCP_PORT', str(value))

        assert ServiceConfig.from_env('gmail').port == value

    @pytest.mark.parametrize('value', ['0', '2592001', '-1', 'nan', 'inf'])
    def test_access_ttl_outside_bounds_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv('GMAIL_OAUTH_ACCESS_TOKEN_TTL_SECONDS', value)
        with pytest.raises(
            ValueError, match='GMAIL_OAUTH_ACCESS_TOKEN_TTL_SECONDS'
        ):
            ServiceConfig.from_env('gmail')

    @pytest.mark.parametrize('value', ['0', '7776001', '-1', 'nan', 'inf'])
    def test_refresh_ttl_outside_bounds_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv('GMAIL_OAUTH_REFRESH_TOKEN_TTL_SECONDS', value)
        with pytest.raises(
            ValueError, match='GMAIL_OAUTH_REFRESH_TOKEN_TTL_SECONDS'
        ):
            ServiceConfig.from_env('gmail')

    @pytest.mark.parametrize('value', ['0', '65536', '-1'])
    def test_port_outside_bounds_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv('GMAIL_MCP_PORT', value)
        with pytest.raises(ValueError, match='GMAIL_MCP_PORT'):
            ServiceConfig.from_env('gmail')

    def test_configuration_is_immutable(self) -> None:
        config = ServiceConfig.from_env('docs')
        with pytest.raises(FrozenInstanceError):
            config.host = 'localhost'
