"""Manage local OAuth metadata."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import TextIO

from google_workspace_mcp.auth.state import (
    ClientMetadata,
    OAuthState,
    OAuthStateError,
    TokenMetadata,
    UnsafeStatePath,
)
from google_workspace_mcp.cli import SERVICES
from google_workspace_mcp.common.config import ServiceConfig


def _parser() -> argparse.ArgumentParser:
    """Build administration argument parser."""
    parser = argparse.ArgumentParser(
        prog='google-mcp-oauth',
        description='Inspect, revoke, and back up local OAuth metadata.',
    )
    parser.add_argument('--service', choices=SERVICES, required=True)
    parser.add_argument('--state-path', type=Path)
    parser.add_argument('--download-path', type=Path)
    parser.add_argument('--legacy-path', type=Path)
    parser.add_argument(
        '--approved-legacy-client-id',
        action='append',
        dest='approved_legacy_client_ids',
    )
    parser.add_argument('--access-token-ttl-seconds', type=int)
    parser.add_argument('--refresh-token-ttl-seconds', type=int)
    resources = parser.add_subparsers(dest='resource', required=True)

    clients = resources.add_parser(
        'clients', help='Client metadata operations'
    )
    client_actions = clients.add_subparsers(dest='action', required=True)
    client_actions.add_parser('list', help='List client metadata')
    revoke_client = client_actions.add_parser(
        'revoke', help='Revoke a client and all of its state'
    )
    revoke_client.add_argument('client_id')

    tokens = resources.add_parser('tokens', help='Token metadata operations')
    token_actions = tokens.add_subparsers(dest='action', required=True)
    list_tokens = token_actions.add_parser('list', help='List token metadata')
    list_tokens.add_argument('--client-id')
    list_tokens.add_argument(
        '--active-only',
        action='store_true',
        help='Hide expired or revoked tokens',
    )
    revoke_token = token_actions.add_parser('revoke', help='Revoke one token')
    revoke_token.add_argument('token_id')

    backup = resources.add_parser(
        'backup', help='Create an online SQLite backup'
    )
    backup.add_argument('destination', type=Path)
    return parser


def _client_payload(client: ClientMetadata) -> dict[str, object]:
    """Build client metadata payload."""
    return {
        'client_id': client.client_id,
        'client_name': client.client_name,
        'created_at': client.created_at,
        'is_static': client.is_static,
        'last_authorized_at': client.last_authorized_at,
        'policy': client.policy,
        'redirect_uris': list(client.redirect_uris),
        'revoked_at': client.revoked_at,
    }


def _token_payload(token: TokenMetadata) -> dict[str, object]:
    """Build token metadata payload."""
    return {
        'capabilities': list(token.capabilities),
        'client_id': token.client_id,
        'expires_at': token.expires_at,
        'issued_at': token.issued_at,
        'policy': token.policy,
        'resource': token.resource,
        'revoked_at': token.revoked_at,
        'token_id': token.token_id,
    }


def _emit(stream: TextIO, payload: object) -> None:
    """Emit compact JSON payload."""
    json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
    stream.write('\n')


def _state_from_args(args: argparse.Namespace) -> OAuthState:
    """Open selected service state."""
    config = ServiceConfig.from_env(args.service)
    state_path = args.state_path or config.oauth_state_path
    approved_legacy_client_ids = (
        config.approved_legacy_client_ids
        if args.approved_legacy_client_ids is None
        else frozenset(args.approved_legacy_client_ids)
    )
    return OAuthState(
        state_path,
        download_path=args.download_path or config.download_path,
        service_id=config.service_id,
        resource=config.public_url,
        legacy_path=args.legacy_path or config.legacy_clients_path,
        approved_legacy_client_ids=approved_legacy_client_ids,
        access_token_ttl_seconds=(
            config.access_token_ttl_seconds
            if args.access_token_ttl_seconds is None
            else args.access_token_ttl_seconds
        ),
        refresh_token_ttl_seconds=(
            config.refresh_token_ttl_seconds
            if args.refresh_token_ttl_seconds is None
            else args.refresh_token_ttl_seconds
        ),
    )


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Execute local metadata operation."""
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    args = _parser().parse_args(argv)
    state: OAuthState | None = None
    try:
        state = _state_from_args(args)
        if args.resource == 'clients' and args.action == 'list':
            clients = [
                _client_payload(client) for client in state.list_clients()
            ]
            _emit(output, clients)
            return 0
        if args.resource == 'clients' and args.action == 'revoke':
            revoked = state.revoke_client(args.client_id)
            client_result: dict[str, object] = {'revoked': revoked}
            if revoked:
                client_result['client_id'] = args.client_id
            _emit(output, client_result)
            return 0 if revoked else 1
        if args.resource == 'tokens' and args.action == 'list':
            tokens = state.list_tokens(include_inactive=not args.active_only)
            if args.client_id is not None:
                tokens = tuple(
                    token
                    for token in tokens
                    if token.client_id == args.client_id
                )
            _emit(output, [_token_payload(token) for token in tokens])
            return 0
        if args.resource == 'tokens' and args.action == 'revoke':
            revoked = state.revoke_token(args.token_id)
            token_result: dict[str, object] = {'revoked': revoked}
            if revoked:
                token_result['token_id'] = args.token_id
            _emit(output, token_result)
            return 0 if revoked else 1
        if args.resource == 'backup':
            destination = state.backup(args.destination)
            _emit(output, {'backup': str(destination)})
            return 0
        raise AssertionError('unhandled OAuth admin command')
    except UnsafeStatePath as exc:
        message = str(exc)
        safe_messages = {
            'OAuth state owner mismatch',
            'existing OAuth state has no owner metadata',
        }
        _emit(
            errors,
            {
                'error': message
                if message in safe_messages
                else 'unsafe OAuth state'
            },
        )
        return 1
    except (OSError, ValueError, OAuthStateError, sqlite3.Error) as exc:
        _emit(errors, {'error': str(exc)})
        return 1
    finally:
        if state is not None:
            state.close()


def _entrypoint() -> None:
    """Run administration entrypoint."""
    raise SystemExit(main())


if __name__ == '__main__':
    _entrypoint()
