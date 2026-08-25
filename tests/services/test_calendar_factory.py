"""Test Calendar extension wiring."""

from __future__ import annotations

from pathlib import Path

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.services.calendar import (
    CALENDAR_SCOPES,
    CalendarExtension,
)
from google_workspace_mcp.services.calendar.factory import create_calendar_app

from .test_calendar_tools import READONLY_TOOLS, TOOL_NAMES


def _config(tmp_path: Path) -> ServiceConfig:
    """Create isolated Calendar config."""
    root = tmp_path / 'calendar'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    return ServiceConfig(
        service_id='calendar',
        public_url='https://127.0.0.1:8432',
        mcp_path='/calendar/mcp',
        host='127.0.0.1',
        port=8432,
        download_path=downloads,
        oauth_state_path=root / 'oauth.db',
        google_token_path=root / 'token.json',
        audit_log_path=root / 'audit.jsonl',
        oauth_login_username='admin',
        oauth_login_password='secret-password',
        allowed_hosts=(),
        forwarded_allow_ips=('127.0.0.1',),
        legacy_clients_path=None,
        approved_legacy_client_ids=frozenset(),
        access_token_ttl_seconds=86400,
        refresh_token_ttl_seconds=2592000,
    )


def test_factory_registers_calendar_tools(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app, server, state = create_calendar_app(config)
    try:
        assert {
            tool.name for tool in server._tool_manager.list_tools()
        } == TOOL_NAMES
        assert set(state.readonly_capabilities) == READONLY_TOOLS
        assert state.service_id == 'calendar'
        assert app is not None
    finally:
        state.close()


def test_extension_owns_calendar_scopes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    extension = CalendarExtension(config)
    assert extension.store.path == config.google_token_path
    assert extension.store.required_scopes == CALENDAR_SCOPES
