"""Check public web assets."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ROOT = PROJECT_ROOT / 'deploy' / 'public'
NGINX_CONFIG = PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp.conf'
NGINX_BOOTSTRAP_CONFIG = (
    PROJECT_ROOT / 'deploy' / 'nginx-google-workspace-mcp-bootstrap.conf'
)


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


def test_prepublication_nginx_serves_static_pages_only() -> None:
    config = NGINX_CONFIG.read_text(encoding='utf-8')

    assert 'server_name __DOMAIN__;' in config
    assert 'listen 443 ssl http2;' in config
    assert 'root /opt/google-workspace-mcp/public;' in config
    assert 'location = /privacy {' in config
    assert 'location /assets/ {' in config
    assert 'proxy_pass' not in config
    assert 'upstream ' not in config


def test_bootstrap_nginx_serves_acme_and_public_pages() -> None:
    config = NGINX_BOOTSTRAP_CONFIG.read_text(encoding='utf-8')

    assert 'listen 80;' in config
    assert 'listen 443' not in config
    assert 'location /.well-known/acme-challenge/ {' in config
    assert 'root /opt/google-workspace-mcp/public;' in config
    assert 'proxy_pass' not in config
