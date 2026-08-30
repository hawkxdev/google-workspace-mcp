"""Check public web assets."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ROOT = PROJECT_ROOT / 'deploy' / 'public'
NGINX_CONFIG = PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp.conf'
NGINX_ACTIVE_INC = (
    PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp-active.inc'
)
NGINX_MAINTENANCE_INC = (
    PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp-maintenance.inc'
)
NGINX_CANDIDATE_CONFIG = (
    PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp-candidate.conf'
)
NGINX_BOOTSTRAP_CONFIG = (
    PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp-bootstrap.conf'
)
SERVICES = ('gmail', 'calendar', 'drive', 'sheets', 'docs')


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


def test_runtime_nginx_preserves_static_and_dynamic_include() -> None:
    config = NGINX_CONFIG.read_text(encoding='utf-8')

    assert 'server_name __DOMAIN__;' in config
    assert 'listen 443 ssl http2;' in config
    assert 'root /opt/google-workspace-mcp/public;' in config
    assert 'location = /privacy {' in config
    assert 'location /assets/ {' in config
    assert 'location /.well-known/acme-challenge/ {' in config
    snippet_include = (
        'include /etc/nginx/snippets/google-workspace-mcp-dynamic.inc;'
    )
    assert snippet_include in config
    assert config.count('return 404;') == 1
    assert config.count('limit_req_zone ') == 1
    assert 'zone=gws_authorize:1m rate=5r/s;' in config
    assert 'form-action $gws_form_action' in config
    assert "form-action 'none'" not in config
    assert 'proxy_pass' not in config

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


def test_active_nginx_snippet_routes_all_five_services() -> None:
    active = NGINX_ACTIVE_INC.read_text(encoding='utf-8')

    for service in SERVICES:
        upstream = f'gws_{service}'
        assert (
            f'location = /.well-known/oauth-protected-resource/{service}/mcp'
            in active
        )
        assert (
            f'location = /.well-known/oauth-authorization-server/{service}'
            in active
        )
        assert f'location = /{service}/oauth/authorize' in active
        assert f'location /{service}/' in active
        assert f'location = /{service}/health' in active
        assert f'location = /{service}/ready' in active

        locations = {
            f'location /{service}/': f'http://{upstream};',
            f'location = /{service}/oauth/authorize': f'http://{upstream};',
            (
                f'location = /.well-known/oauth-authorization-server/{service}'
            ): f'http://{upstream};',
            (
                'location = /.well-known/oauth-protected-resource/'
                f'{service}/mcp'
            ): f'http://{upstream};',
            f'location = /{service}/health': f'http://{upstream}/health;',
            f'location = /{service}/ready': f'http://{upstream}/ready;',
        }
        for header, target in locations.items():
            block = _nginx_block(active, header)
            assert f'proxy_pass {target}' in block
            assert 'proxy_http_version 1.1;' in block
            assert 'proxy_set_header Connection "";' in block

        authorize = _nginx_block(
            active, f'location = /{service}/oauth/authorize'
        )
        assert 'limit_req zone=gws_authorize burst=10 nodelay;' in authorize
        stream = _nginx_block(active, f'location /{service}/')
        assert 'proxy_buffering off;' in stream
        assert 'proxy_request_buffering off;' in stream
        assert 'proxy_read_timeout 300s;' in stream
        assert 'proxy_send_timeout 300s;' in stream

        for line in active.splitlines():
            stripped = line.strip()
            old_prm = (
                'location = /.well-known/oauth-protected-resource/'
                f'{service} {{'
            )
            assert stripped != old_prm

    for root_alias in (
        '/register',
        '/authorize',
        '/token',
        '/.well-known/oauth-protected-resource',
        '/.well-known/oauth-authorization-server',
    ):
        for line in active.splitlines():
            stripped = line.strip()
            assert stripped != f'location = {root_alias} {{'
            assert stripped != f'location {root_alias} {{'

    assert active.count('proxy_pass ') == 30
    assert active.count('proxy_http_version 1.1;') == 30
    assert active.count('proxy_set_header Connection "";') == 30


def test_maintenance_nginx_snippet_returns_controlled_503() -> None:
    maintenance = NGINX_MAINTENANCE_INC.read_text(encoding='utf-8')

    for service in SERVICES:
        assert f'location /{service}/' in maintenance
        block = _nginx_block(maintenance, f'location /{service}/')
        assert 'return 503;' in block
        assert 'Retry-After' in block

    for well_known in (
        '/.well-known/oauth-protected-resource/',
        '/.well-known/oauth-authorization-server/',
    ):
        assert f'location {well_known}' in maintenance
        block = _nginx_block(maintenance, f'location {well_known}')
        assert 'return 503;' in block
        assert 'Retry-After' in block

    assert 'proxy_pass' not in maintenance
    assert 'location = /privacy' not in maintenance
    assert 'location = / ' not in maintenance
    assert 'location /assets/' not in maintenance
    assert 'acme-challenge' not in maintenance


def test_candidate_nginx_config_matches_loopback_tls_contract() -> None:
    candidate = NGINX_CANDIDATE_CONFIG.read_text(encoding='utf-8')

    assert 'listen 127.0.0.1:9443 ssl;' in candidate
    assert (
        'ssl_certificate /etc/letsencrypt/live/'
        'mcp.hawkxdev.dev/fullchain.pem;' in candidate
    )
    assert (
        'ssl_certificate_key /etc/letsencrypt/live/'
        'mcp.hawkxdev.dev/privkey.pem;' in candidate
    )
    assert (
        'include /etc/nginx/snippets/google-workspace-mcp-active.inc;'
        in candidate
    )
    assert 'server_name mcp.hawkxdev.dev;' in candidate

    for forbidden in (
        'listen 80',
        'listen 443',
        'listen [::]',
        'listen 0.0.0.0',
    ):
        assert forbidden not in candidate

    assert 'location = / {' in candidate
    assert 'location = /privacy {' in candidate
    assert 'location /assets/ {' in candidate


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
