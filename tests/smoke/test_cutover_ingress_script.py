"""Check cutover ingress script."""

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = PROJECT_ROOT / 'deploy'
INGRESS_SCRIPT = DEPLOY_ROOT / 'check-cutover-ingress.sh'
SERVICES = ('gmail', 'calendar', 'drive', 'sheets', 'docs')


def test_cutover_ingress_script_exists_and_is_executable() -> None:
    assert INGRESS_SCRIPT.exists()
    assert os.access(INGRESS_SCRIPT, os.X_OK)


def test_cutover_ingress_script_syntax_valid() -> None:
    result = subprocess.run(
        ['bash', '-n', str(INGRESS_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert not result.stderr


def test_cutover_ingress_script_requires_valid_mode() -> None:
    no_args = subprocess.run(
        [str(INGRESS_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert no_args.returncode != 0
    usage_output = (no_args.stderr + no_args.stdout).lower()
    assert 'usage' in usage_output

    invalid_mode = subprocess.run(
        [str(INGRESS_SCRIPT), 'unknown-mode'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_mode.returncode != 0

    help_arg = subprocess.run(
        [str(INGRESS_SCRIPT), '--help'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_arg.returncode == 0
    assert 'usage' in help_arg.stdout.lower()


def test_cutover_ingress_script_satisfies_contract() -> None:
    script = INGRESS_SCRIPT.read_text(encoding='utf-8')

    assert '#!/usr/bin/env bash' in script
    assert 'set -euo pipefail' in script

    assert 'candidate' in script
    assert 'public' in script

    assert '--connect-to' in script
    assert 'DOMAIN="mcp.hawkxdev.dev"' in script
    assert 'CANDIDATE_ADDR="127.0.0.1:9443"' in script
    assert '${DOMAIN}:443:${CANDIDATE_ADDR}' in script
    for service in SERVICES:
        assert service in script

    assert '/.well-known/oauth-protected-resource/${svc}/mcp' in script
    assert '/.well-known/oauth-authorization-server/${svc}' in script
    assert '/${svc}/mcp' in script
    assert '/${svc}/health' in script
    assert '/${svc}/ready' in script
    assert 'Expected 401 for ready' in script
    assert '/${svc}/oauth/register' in script
    assert '/${svc}/oauth/authorize' in script
    assert '/${svc}/oauth/token' in script
    for old_path in (
        '/.well-known/oauth-protected-resource',
        '/.well-known/oauth-authorization-server',
        '/register',
        '/authorize',
        '/token',
    ):
        assert old_path in script

    for method in ('GET', 'HEAD', 'POST'):
        assert method in script

    assert 'oauth_state.sqlite3' in script
