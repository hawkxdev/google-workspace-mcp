"""Check public web assets."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ROOT = PROJECT_ROOT / 'deploy' / 'public'
NGINX_CONFIG = PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp.conf'
NGINX_BOOTSTRAP_CONFIG = (
    PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp-bootstrap.conf'
)


def _nginx_block(config: str, header: str) -> str:
    """Return one nginx block."""
    marker = f'{header} {{'
    start = config.index(marker)
    depth = 0
    for index in range(start, len(config)):
        if config[index] == '{':
            depth += 1
        elif config[index] == '}':
            depth -= 1
            if depth == 0:
                return config[start : index + 1]
    raise AssertionError(f'unclosed nginx block: {header}')


def test_landing_page_explains_product_boundary() -> None:
    html = (PUBLIC_ROOT / 'index.html').read_text(encoding='utf-8')

    assert '<title>Hawkx Workspace MCP</title>' in html
    assert 'This is not a Google product.' in html
    assert '<span>HAWKXDEV</span>' in html
    assert 'href="/privacy"' in html
    for service in ('Gmail', 'Calendar', 'Drive', 'Sheets', 'Docs'):
        assert service in html


def test_privacy_page_discloses_google_data_contract() -> None:
    html = (PUBLIC_ROOT / 'privacy' / 'index.html').read_text(encoding='utf-8')

    for heading in (
        'Google data we access',
        'How Google data is used',
        'Storage and retention',
        'Sharing and disclosure',
        'Your controls and deletion',
        'Security',
    ):
        assert heading in html
    assert 'Google API Services User Data Policy' in html
    assert 'Limited Use' in html
    assert 'mailto:hawkxdev@gmail.com' in html


def test_public_pages_load_only_local_assets() -> None:
    for path in (
        PUBLIC_ROOT / 'index.html',
        PUBLIC_ROOT / 'privacy/index.html',
    ):
        html = path.read_text(encoding='utf-8')

        assert '<script' not in html
        assert 'src="http://' not in html
        assert 'src="https://' not in html
        assert 'href="/assets/site.css"' in html
        assert 'rel="icon" href="data:,"' in html


def test_styles_preserve_keyboard_and_motion_preferences() -> None:
    css = (PUBLIC_ROOT / 'assets' / 'site.css').read_text(encoding='utf-8')

    assert ':focus-visible' in css
    assert '@media (prefers-reduced-motion: reduce)' in css


def test_runtime_nginx_preserves_static_and_service_routes() -> None:
    config = NGINX_CONFIG.read_text(encoding='utf-8')

    assert 'server_name __DOMAIN__;' in config
    assert 'listen 443 ssl http2;' in config
    assert 'root /opt/google-workspace-mcp/public;' in config
    assert 'location = /privacy {' in config
    assert 'location /assets/ {' in config
    assert 'location /.well-known/acme-challenge/ {' in config
    assert config.count('return 404;') == 1
    assert config.count('limit_req_zone ') == 1
    assert 'zone=gws_authorize:1m rate=5r/s;' in config
    assert 'form-action $gws_form_action' in config
    assert "form-action 'none'" not in config

    for service, port in (
        ('gmail', 8431),
        ('calendar', 8432),
        ('drive', 8433),
        ('sheets', 8434),
        ('docs', 8435),
    ):
        upstream = f'gws_{service}'
        assert f'upstream {upstream} {{' in config
        assert f'server 127.0.0.1:{port};' in config
        locations = {
            f'location /{service}/': f'http://{upstream};',
            f'location = /{service}/oauth/authorize': (f'http://{upstream};'),
            (
                f'location = /.well-known/oauth-authorization-server/{service}'
            ): f'http://{upstream};',
            (
                f'location = /.well-known/oauth-protected-resource/{service}'
            ): f'http://{upstream};',
            f'location = /{service}/health': f'http://{upstream}/health;',
            f'location = /{service}/ready': f'http://{upstream}/ready;',
        }
        for header, target in locations.items():
            block = _nginx_block(config, header)
            assert f'proxy_pass {target}' in block
            assert 'proxy_http_version 1.1;' in block
            assert 'proxy_set_header Connection "";' in block

        authorize = _nginx_block(
            config, f'location = /{service}/oauth/authorize'
        )
        assert 'limit_req zone=gws_authorize burst=10 nodelay;' in authorize
        stream = _nginx_block(config, f'location /{service}/')
        assert 'proxy_buffering off;' in stream
        assert 'proxy_request_buffering off;' in stream
        assert 'proxy_read_timeout 300s;' in stream
        assert 'proxy_send_timeout 300s;' in stream

    assert config.count('proxy_pass ') == 30
    assert config.count('proxy_http_version 1.1;') == 30
    assert config.count('proxy_set_header Connection "";') == 30


def test_runtime_nginx_allows_registered_oauth_redirect_schemes() -> None:
    config = NGINX_CONFIG.read_text(encoding='utf-8')

    csp = _nginx_block(config, 'map $uri $gws_form_action')
    assert 'default ' in csp
    assert "'self'" in csp
    assert '~^/(gmail|calendar|drive|sheets|docs)/oauth/authorize$' in csp
    assert 'https:' in csp
    assert 'http://127.0.0.1:*' in csp
    assert 'http://localhost:*' in csp
    assert 'add_header Content-Security-Policy ' in config
    assert 'form-action $gws_form_action' in config


def test_bootstrap_nginx_serves_acme_and_public_pages() -> None:
    config = NGINX_BOOTSTRAP_CONFIG.read_text(encoding='utf-8')

    assert 'listen 80;' in config
    assert 'listen 443' not in config
    assert 'location /.well-known/acme-challenge/ {' in config
    assert 'root /opt/google-workspace-mcp/public;' in config
    assert 'proxy_pass' not in config
