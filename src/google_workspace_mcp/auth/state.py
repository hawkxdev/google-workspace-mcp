"""Manage OAuth state."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# === OAuth configuration constants ===
SCHEMA_VERSION = 2
AUTHORIZATION_CODE_TTL_SECONDS = 300
MAX_ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
_STATE_INITIALIZATION_LOCK = threading.Lock()

MCP_READONLY_V1 = 'mcp_readonly_v1'
LEGACY_FULL = 'legacy_full'
REAUTHORIZATION_REQUIRED = 'reauthorization_required'


# === Database schema definition ===
_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    secret_verifier BLOB,
    policy TEXT NOT NULL,
    is_static INTEGER NOT NULL CHECK (is_static IN (0, 1)),
    client_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_authorized_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS client_redirect_uris (
    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    PRIMARY KEY (client_id, redirect_uri)
);
CREATE TABLE IF NOT EXISTS authorization_codes (
    code_verifier BLOB PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    policy TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token_id TEXT PRIMARY KEY,
    secret_verifier BLOB NOT NULL,
    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    policy TEXT NOT NULL,
    resource TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS access_token_capabilities (
    token_id TEXT NOT NULL REFERENCES access_tokens(token_id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    PRIMARY KEY (token_id, capability)
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    refresh_id TEXT PRIMARY KEY,
    secret_verifier BLOB NOT NULL,
    family_id TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    access_token_id TEXT REFERENCES access_tokens(token_id) ON DELETE CASCADE,
    policy TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    resource TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS refresh_tokens_family
    ON refresh_tokens (family_id);
CREATE TABLE IF NOT EXISTS state_owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    service_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_metadata (
    name TEXT PRIMARY KEY,
    source_digest TEXT NOT NULL,
    imported_count INTEGER NOT NULL,
    completed_at REAL NOT NULL,
    cleanup_pending INTEGER NOT NULL CHECK (cleanup_pending IN (0, 1))
);
"""


# === Resource helper functions ===
def canonicalize_resource(value: str) -> str:
    """Canonicalize resource URL."""
    if value.endswith('/') and not value.endswith('//'):
        head = value[:-1]
        separator = head.find('://')
        if separator != -1 and '/' not in head[separator + 3 :]:
            return head
    return value


# === OAuth state errors ===
class OAuthStateError(RuntimeError):
    """Base OAuth state error."""


class UnsafeStatePath(OAuthStateError):
    """Unsafe state path error."""


class InvalidClient(OAuthStateError):
    """Invalid client error."""


class InvalidGrant(OAuthStateError):
    """Invalid grant error."""


class _RefreshReplay(Exception):
    """Refresh replay signal."""


class InvalidTarget(OAuthStateError):
    """Invalid resource target."""


# === OAuth state records ===
@dataclass(frozen=True)
class ClientMetadata:
    """Registered client metadata."""

    client_id: str
    redirect_uris: tuple[str, ...]
    policy: str
    client_name: str
    created_at: float
    last_authorized_at: float | None
    revoked_at: float | None
    is_static: bool
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegisteredClient:
    """Registered client credentials."""

    client: ClientMetadata
    client_secret: str


@dataclass(frozen=True)
class TokenMetadata:
    """Issued access metadata."""

    token_id: str
    client_id: str
    policy: str
    capabilities: tuple[str, ...]
    resource: str
    issued_at: float
    expires_at: float
    revoked_at: float | None


@dataclass(frozen=True)
class RefreshTokenMetadata:
    """Refresh credential metadata."""

    refresh_id: str
    family_id: str
    client_id: str
    access_token_id: str | None
    policy: str
    capabilities: tuple[str, ...]
    resource: str
    issued_at: float
    expires_at: float
    consumed_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class IssuedAccessToken:
    """Issued access credentials."""

    access_token: str
    token: TokenMetadata
    refresh_token: str | None = None
    refresh: RefreshTokenMetadata | None = None


@dataclass(frozen=True)
class MigrationRecord:
    """Legacy migration record."""

    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]
    client_name: str
    created_at: float


# === OAuth state implementation ===
class OAuthState:
    """Persistent OAuth state."""

    def __init__(
        self,
        path: str | Path,
        *,
        download_path: str | Path,
        service_id: str,
        resource: str,
        readonly_capabilities: tuple[str, ...] = (),
        legacy_path: str | Path | None = None,
        approved_legacy_client_ids: frozenset[str] = frozenset(),
        access_token_ttl_seconds: int = 86_400,
        refresh_token_ttl_seconds: int = 2_592_000,
        clock: Callable[[], float] | None = None,
        static_client_authenticator: Callable[[str, str], bool] | None = None,
    ) -> None:
        """Configure OAuth state."""
        # 1. Validate token lifetimes
        if not 1 <= access_token_ttl_seconds <= MAX_ACCESS_TOKEN_TTL_SECONDS:
            raise ValueError(
                'access_token_ttl_seconds must be between 1 and 2592000'
            )
        if not 1 <= refresh_token_ttl_seconds <= MAX_REFRESH_TOKEN_TTL_SECONDS:
            raise ValueError(
                'refresh_token_ttl_seconds must be between 1 and 7776000'
            )
        # 2. Normalize state settings
        self.path = Path(path).expanduser().absolute()
        self.download_path = Path(download_path).expanduser().absolute()
        self.service_id = service_id
        self.resource = canonicalize_resource(resource)
        self._readonly_capabilities = readonly_capabilities
        self.legacy_path = (
            Path(legacy_path).expanduser().absolute()
            if legacy_path is not None
            else None
        )
        self.approved_legacy_client_ids = approved_legacy_client_ids
        self.access_token_ttl_seconds = access_token_ttl_seconds
        self.refresh_token_ttl_seconds = refresh_token_ttl_seconds
        self._clock = clock or time.time
        self._static_client_authenticator = static_client_authenticator
        self._lock = threading.RLock()
        self._closed = False

        # 3. Open state database
        self._assert_outside_downloads(self.path)
        self._prepare_secure_directory(self.path.parent)
        self._validate_secure_file(self.path, allow_missing=True)
        with self._state_initialization_lock():
            validated_identity = self._validate_state_targets()
            created = self._create_database_file()
            expected_identity = (
                validated_identity or self._state_file_identity()
            )
            if not created:
                self._preflight_existing_state_owner(expected_identity)
            self._connection = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            try:
                self._assert_state_file_identity(expected_identity)
                if created:
                    self._initialize_schema(initialize_owner=True)
                else:
                    self._assert_connected_state_owner()
                    self._initialize_schema(initialize_owner=False)
                self._configure_connection()
                self._secure_state_files()
            except BaseException:
                self._connection.close()
                self._closed = True
                raise

    def migrate_legacy(self) -> None:
        """Migrate legacy client data."""
        self._migrate_legacy_once()

    @property
    def schema_version(self) -> int:
        """Read schema version."""
        with self._lock:
            return int(
                self._connection.execute('PRAGMA user_version').fetchone()[0]
            )

    @property
    def readonly_capabilities(self) -> tuple[str, ...]:
        """Read configured capabilities."""
        return self._readonly_capabilities

    def table_names(self) -> set[str]:
        """List database tables."""
        with self._lock:
            rows = self._connection.execute(
                'SELECT name FROM sqlite_master '
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def close(self) -> None:
        """Close database state."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> OAuthState:
        """Enter state context."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Exit state context."""
        self.close()

    def register_client(
        self,
        redirect_uris: Sequence[str],
        *,
        client_name: str = 'Google Workspace MCP Client',
    ) -> RegisteredClient:
        """Register dynamic client."""
        redirects = self._normalize_redirects(redirect_uris)
        now = self._clock()
        with self._transaction() as connection:
            while True:
                client_id = f'gwmcp-{self.service_id}-{secrets.token_hex(8)}'
                exists = connection.execute(
                    'SELECT 1 FROM clients WHERE client_id = ?', (client_id,)
                ).fetchone()
                if exists is None:
                    break
            client_secret = secrets.token_hex(32)
            connection.execute(
                'INSERT INTO clients '
                '(client_id, secret_verifier, policy, is_static, client_name, '
                'created_at) VALUES (?, ?, ?, 0, ?, ?)',
                (
                    client_id,
                    _secret_verifier('client', client_secret),
                    MCP_READONLY_V1,
                    client_name,
                    now,
                ),
            )
            connection.executemany(
                'INSERT INTO client_redirect_uris (client_id, redirect_uri) '
                'VALUES (?, ?)',
                ((client_id, uri) for uri in redirects),
            )
        client = self.get_client(client_id)
        assert client is not None
        return RegisteredClient(client=client, client_secret=client_secret)

    def ensure_static_client(
        self,
        client_id: str,
        redirect_uris: Sequence[str],
        *,
        policy: str,
        capabilities: Sequence[str],
        client_name: str = 'Google Workspace MCP Static Client',
    ) -> ClientMetadata:
        """Ensure static client."""
        del capabilities
        if not client_id:
            raise ValueError('static client_id must not be empty')
        self._validate_policy(policy)
        redirects = self._normalize_redirects(redirect_uris)
        now = self._clock()
        with self._transaction() as connection:
            existing = connection.execute(
                'SELECT * FROM clients WHERE client_id = ?', (client_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    'INSERT INTO clients '
                    '(client_id, secret_verifier, policy, is_static, '
                    'client_name, created_at) VALUES (?, NULL, ?, 1, ?, ?)',
                    (client_id, policy, client_name, now),
                )
                connection.executemany(
                    'INSERT INTO client_redirect_uris '
                    '(client_id, redirect_uri) VALUES (?, ?)',
                    ((client_id, uri) for uri in redirects),
                )
            elif not bool(existing['is_static']):
                raise ValueError(
                    f'configured static client_id {client_id!r} is already dynamic'
                )
            else:
                connection.execute(
                    'UPDATE clients SET policy = ?, client_name = ? '
                    'WHERE client_id = ?',
                    (policy, client_name, client_id),
                )
                connection.execute(
                    'DELETE FROM client_redirect_uris WHERE client_id = ?',
                    (client_id,),
                )
                connection.executemany(
                    'INSERT INTO client_redirect_uris '
                    '(client_id, redirect_uri) VALUES (?, ?)',
                    ((client_id, uri) for uri in redirects),
                )
        client = self.get_client(client_id)
        assert client is not None
        return client

    def get_client(self, client_id: str) -> ClientMetadata | None:
        """Load client metadata."""
        with self._lock:
            row = self._connection.execute(
                'SELECT * FROM clients WHERE client_id = ?', (client_id,)
            ).fetchone()
            if row is None:
                return None
            redirects = self._redirects_for(self._connection, client_id)
        return _client_from_row(row, redirects, self._readonly_capabilities)

    def list_clients(self) -> tuple[ClientMetadata, ...]:
        """List registered clients."""
        with self._lock:
            rows = self._connection.execute(
                'SELECT * FROM clients ORDER BY created_at, client_id'
            ).fetchall()
            return tuple(
                _client_from_row(
                    row,
                    self._redirects_for(self._connection, row['client_id']),
                    self._readonly_capabilities,
                )
                for row in rows
            )

    def verify_client_secret(self, client_id: str, client_secret: str) -> bool:
        """Verify client secret."""
        if not client_secret:
            return False
        with self._lock:
            row = self._connection.execute(
                'SELECT secret_verifier, is_static, revoked_at FROM clients '
                'WHERE client_id = ?',
                (client_id,),
            ).fetchone()
        if row is None or row['revoked_at'] is not None:
            return False
        if bool(row['is_static']):
            authenticator = self._static_client_authenticator
            return bool(
                authenticator is not None
                and authenticator(client_id, client_secret)
            )
        verifier = row['secret_verifier']
        return bool(
            verifier is not None
            and hmac.compare_digest(
                bytes(verifier), _secret_verifier('client', client_secret)
            )
        )

    def client_redirect_uri_allowed(self, client_id: str, uri: str) -> bool:
        """Check redirect URI."""
        with self._lock:
            row = self._connection.execute(
                'SELECT 1 FROM clients AS c '
                'JOIN client_redirect_uris AS r USING (client_id) '
                'WHERE c.client_id = ? AND c.revoked_at IS NULL '
                'AND r.redirect_uri = ?',
                (client_id, uri),
            ).fetchone()
        return row is not None

    def can_issue_authorization_code(self, client_id: str) -> bool:
        """Check authorization eligibility."""
        client = self.get_client(client_id)
        return bool(
            client is not None
            and client.revoked_at is None
            and client.policy != REAUTHORIZATION_REQUIRED
        )

    def issue_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
        fresh_reauthorization: bool = False,
    ) -> str:
        """Issue authorization code."""
        # 1. Validate authorization request
        if not code_challenge:
            raise InvalidGrant('missing PKCE challenge')
        resource = canonicalize_resource(resource)
        now = self._clock()
        code = secrets.token_urlsafe(32)
        with self._transaction() as connection:
            # 2. Load client policy
            client = connection.execute(
                'SELECT policy, revoked_at FROM clients WHERE client_id = ?',
                (client_id,),
            ).fetchone()
            if client is None or client['revoked_at'] is not None:
                raise InvalidClient('unknown or revoked client')
            policy = str(client['policy'])
            if policy == REAUTHORIZATION_REQUIRED:
                if not fresh_reauthorization:
                    raise InvalidClient(
                        'client requires fresh reauthorization'
                    )
                policy = MCP_READONLY_V1
                connection.execute(
                    'UPDATE clients SET policy = ?, last_authorized_at = ? '
                    'WHERE client_id = ?',
                    (policy, now, client_id),
                )
            capabilities = _capabilities_for_policy(
                policy, self._readonly_capabilities
            )
            allowed = connection.execute(
                'SELECT 1 FROM client_redirect_uris '
                'WHERE client_id = ? AND redirect_uri = ?',
                (client_id, redirect_uri),
            ).fetchone()
            if allowed is None:
                raise InvalidGrant('redirect URI is not registered')
            # 3. Persist authorization code
            connection.execute(
                'INSERT INTO authorization_codes '
                '(code_verifier, client_id, redirect_uri, code_challenge, '
                'resource, policy, capabilities_json, created_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    _secret_verifier('authorization-code', code),
                    client_id,
                    redirect_uri,
                    code_challenge,
                    resource,
                    policy,
                    json.dumps(capabilities, separators=(',', ':')),
                    now,
                    now + AUTHORIZATION_CODE_TTL_SECONDS,
                ),
            )
        return code

    def authorization_code_active(self, code: str) -> bool:
        """Check active authorization code."""
        now = self._clock()
        with self._lock:
            row = self._connection.execute(
                'SELECT expires_at, consumed_at, revoked_at '
                'FROM authorization_codes WHERE code_verifier = ?',
                (_secret_verifier('authorization-code', code),),
            ).fetchone()
        return bool(
            row is not None
            and row['consumed_at'] is None
            and row['revoked_at'] is None
            and now < float(row['expires_at'])
        )

    def redeem_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code_verifier: str,
        resource: str,
    ) -> IssuedAccessToken:
        """Redeem authorization code."""
        resource = canonicalize_resource(resource)
        now = self._clock()
        with self._transaction() as connection:
            # 1. Authenticate client credentials
            self._authenticate_client_in_transaction(
                connection, client_id, client_secret
            )
            # 2. Validate authorization code
            row = connection.execute(
                'SELECT * FROM authorization_codes WHERE code_verifier = ?',
                (_secret_verifier('authorization-code', code),),
            ).fetchone()
            if row is None:
                raise InvalidGrant('unknown authorization code')
            if row['client_id'] != client_id:
                raise InvalidGrant('authorization code client mismatch')
            if row['redirect_uri'] != redirect_uri:
                raise InvalidGrant('authorization code redirect mismatch')
            if canonicalize_resource(row['resource']) != resource:
                raise InvalidTarget('authorization code resource mismatch')
            if now >= float(row['expires_at']):
                raise InvalidGrant('authorization code expired')
            computed_challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(code_verifier.encode()).digest()
                )
                .rstrip(b'=')
                .decode()
            )
            if not hmac.compare_digest(
                computed_challenge, row['code_challenge']
            ):
                raise InvalidGrant('PKCE verification failed')

            # 3. Issue token pair
            capabilities = tuple(json.loads(row['capabilities_json']))
            issued = self._insert_access_token(
                connection,
                client_id=client_id,
                policy=row['policy'],
                capabilities=capabilities,
                resource=resource,
                now=now,
            )
            updated = connection.execute(
                'UPDATE authorization_codes SET consumed_at = ? '
                'WHERE code_verifier = ? AND consumed_at IS NULL '
                'AND revoked_at IS NULL',
                (now, row['code_verifier']),
            )
            if updated.rowcount != 1:
                raise InvalidGrant('authorization code was already consumed')
            connection.execute(
                'UPDATE clients SET last_authorized_at = ? WHERE client_id = ?',
                (now, client_id),
            )
            raw_refresh, refresh_metadata = self._insert_refresh_token(
                connection,
                client_id=client_id,
                family_id=secrets.token_urlsafe(18),
                access_token_id=issued.token.token_id,
                policy=row['policy'],
                capabilities=capabilities,
                resource=resource,
                now=now,
            )
            return IssuedAccessToken(
                access_token=issued.access_token,
                token=issued.token,
                refresh_token=raw_refresh,
                refresh=refresh_metadata,
            )

    def issue_access_token(
        self,
        *,
        client_id: str,
        resource: str,
    ) -> IssuedAccessToken:
        """Issue access token."""
        resource = canonicalize_resource(resource)
        now = self._clock()
        with self._transaction() as connection:
            row = connection.execute(
                'SELECT revoked_at, policy FROM clients WHERE client_id = ?',
                (client_id,),
            ).fetchone()
            if (
                row is None
                or row['revoked_at'] is not None
                or row['policy'] == REAUTHORIZATION_REQUIRED
            ):
                raise InvalidClient('unknown, revoked, or unauthorized client')
            policy = str(row['policy'])
            return self._insert_access_token(
                connection,
                client_id=client_id,
                policy=policy,
                capabilities=_capabilities_for_policy(
                    policy, self._readonly_capabilities
                ),
                resource=resource,
                now=now,
            )

    def redeem_refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        resource: str,
        audit_hook: Callable[[IssuedAccessToken], None] | None = None,
    ) -> IssuedAccessToken:
        """Redeem refresh token."""
        # 1. Parse refresh token
        resource = canonicalize_resource(resource)
        parsed = _parse_refresh_token(refresh_token)
        if parsed is None:
            raise InvalidGrant('malformed refresh token')
        refresh_id, secret = parsed
        now = self._clock()
        replayed_family: str | None = None
        try:
            with self._transaction() as connection:
                # 2. Validate stored credential
                row = connection.execute(
                    'SELECT r.*, c.revoked_at AS client_revoked_at, '
                    'c.policy AS client_policy FROM refresh_tokens AS r '
                    'JOIN clients AS c USING (client_id) WHERE refresh_id = ?',
                    (refresh_id,),
                ).fetchone()
                if row is None or not hmac.compare_digest(
                    bytes(row['secret_verifier']),
                    _secret_verifier('refresh-token', secret),
                ):
                    raise InvalidGrant('unknown refresh token')
                if row['client_id'] != client_id:
                    raise InvalidGrant('refresh token client mismatch')
                if row['consumed_at'] is not None:
                    # === Refresh replay handling ===
                    replayed_family = str(row['family_id'])
                    raise _RefreshReplay
                if row['revoked_at'] is not None:
                    raise InvalidGrant('refresh token is revoked')
                if row['client_revoked_at'] is not None:
                    raise InvalidClient(
                        'unknown, revoked, or unauthorized client'
                    )
                if now >= float(row['expires_at']):
                    raise InvalidGrant('refresh token expired')
                if canonicalize_resource(row['resource']) != resource:
                    raise InvalidTarget('refresh token resource mismatch')
                policy = str(row['policy'])
                if policy == REAUTHORIZATION_REQUIRED:
                    raise InvalidClient(
                        'unknown, revoked, or unauthorized client'
                    )
                if str(row['client_policy']) != policy:
                    # === OAuth policy validation ===
                    raise InvalidGrant(
                        'client policy changed since authorization'
                    )
                # 3. Rotate token pair
                capabilities = tuple(json.loads(row['capabilities_json']))
                issued = self._insert_access_token(
                    connection,
                    client_id=client_id,
                    policy=policy,
                    capabilities=capabilities,
                    resource=resource,
                    now=now,
                )
                consumed = connection.execute(
                    'UPDATE refresh_tokens SET consumed_at = ? '
                    'WHERE refresh_id = ? AND consumed_at IS NULL '
                    'AND revoked_at IS NULL',
                    (now, refresh_id),
                )
                if consumed.rowcount != 1:
                    raise InvalidGrant('refresh token was already used')
                raw_refresh, refresh_metadata = self._insert_refresh_token(
                    connection,
                    client_id=client_id,
                    family_id=str(row['family_id']),
                    access_token_id=issued.token.token_id,
                    policy=policy,
                    capabilities=capabilities,
                    resource=resource,
                    now=now,
                )
                rotated = IssuedAccessToken(
                    access_token=issued.access_token,
                    token=issued.token,
                    refresh_token=raw_refresh,
                    refresh=refresh_metadata,
                )
                # 4. Record rotation audit
                if audit_hook is not None:
                    audit_hook(rotated)
                return rotated
        # 5. Revoke replay family
        except _RefreshReplay:
            pass
        with self._transaction() as connection:
            self._revoke_refresh_family(connection, str(replayed_family), now)
        raise InvalidGrant('refresh token was already used')

    def list_refresh_tokens(self) -> tuple[RefreshTokenMetadata, ...]:
        """List refresh credentials."""
        with self._lock:
            rows = self._connection.execute(
                'SELECT * FROM refresh_tokens ORDER BY issued_at, refresh_id'
            ).fetchall()
        return tuple(_refresh_from_row(row) for row in rows)

    def revoke_refresh_token(self, refresh_id: str) -> bool:
        """Revoke refresh credential."""
        now = self._clock()
        with self._transaction() as connection:
            result = connection.execute(
                'UPDATE refresh_tokens SET revoked_at = ? '
                'WHERE refresh_id = ? AND revoked_at IS NULL',
                (now, refresh_id),
            )
        return result.rowcount == 1

    def lookup_access_token(self, access_token: str) -> TokenMetadata | None:
        """Lookup access token."""
        parsed = _parse_access_token(access_token)
        if parsed is None:
            return None
        token_id, secret = parsed
        now = self._clock()
        with self._lock:
            row = self._connection.execute(
                'SELECT t.*, c.revoked_at AS client_revoked_at '
                'FROM access_tokens AS t '
                'JOIN clients AS c USING (client_id) '
                'WHERE token_id = ?',
                (token_id,),
            ).fetchone()
            if row is None:
                return None
            if (
                row['revoked_at'] is not None
                or row['client_revoked_at'] is not None
                or now >= float(row['expires_at'])
                or not hmac.compare_digest(
                    bytes(row['secret_verifier']),
                    _secret_verifier('access-token', secret),
                )
            ):
                return None
            capabilities = self._token_capabilities(self._connection, token_id)
        return _token_from_row(row, capabilities)

    def list_tokens(
        self,
        *,
        include_inactive: bool = True,
    ) -> tuple[TokenMetadata, ...]:
        """List access tokens."""
        now = self._clock()
        with self._lock:
            rows = self._connection.execute(
                'SELECT t.*, c.revoked_at AS client_revoked_at '
                'FROM access_tokens AS t JOIN clients AS c USING (client_id) '
                'ORDER BY t.issued_at, t.token_id'
            ).fetchall()
            tokens = []
            for row in rows:
                if not include_inactive and (
                    row['revoked_at'] is not None
                    or row['client_revoked_at'] is not None
                    or now >= float(row['expires_at'])
                ):
                    continue
                tokens.append(
                    _token_from_row(
                        row,
                        self._token_capabilities(
                            self._connection, row['token_id']
                        ),
                    )
                )
        return tuple(tokens)

    def revoke_token(self, token_id: str) -> bool:
        """Revoke access token."""
        now = self._clock()
        with self._transaction() as connection:
            result = connection.execute(
                'UPDATE access_tokens SET revoked_at = ? '
                'WHERE token_id = ? AND revoked_at IS NULL',
                (now, token_id),
            )
        return result.rowcount == 1

    def revoke_client(self, client_id: str) -> bool:
        """Revoke client credentials."""
        now = self._clock()
        with self._transaction() as connection:
            result = connection.execute(
                'UPDATE clients SET revoked_at = ? '
                'WHERE client_id = ? AND revoked_at IS NULL',
                (now, client_id),
            )
            if result.rowcount != 1:
                return False
            connection.execute(
                'UPDATE authorization_codes SET revoked_at = ? '
                'WHERE client_id = ? AND revoked_at IS NULL '
                'AND consumed_at IS NULL',
                (now, client_id),
            )
            connection.execute(
                'UPDATE access_tokens SET revoked_at = ? '
                'WHERE client_id = ? AND revoked_at IS NULL',
                (now, client_id),
            )
            connection.execute(
                'UPDATE refresh_tokens SET revoked_at = ? '
                'WHERE client_id = ? AND revoked_at IS NULL',
                (now, client_id),
            )
        return True

    def backup(self, destination: str | Path) -> Path:
        """Create state backup."""
        # 1. Validate backup target
        target = Path(destination).expanduser().absolute()
        live_paths = {
            self.path,
            Path(f'{self.path}-wal'),
            Path(f'{self.path}-shm'),
        }
        if target in live_paths:
            raise UnsafeStatePath(
                f'backup destination overlaps live OAuth state: {target}'
            )
        self._assert_outside_downloads(target)
        self._prepare_secure_directory(target.parent)
        self._validate_secure_file(target, allow_missing=True)
        for suffix in ('-wal', '-shm'):
            sidecar = Path(f'{target}{suffix}')
            if not os.path.lexists(sidecar):
                continue
            try:
                self._validate_secure_file(sidecar, allow_missing=False)
            except UnsafeStatePath as exc:
                raise UnsafeStatePath(
                    f'backup destination sidecar is unsafe: {sidecar}'
                ) from exc
            raise UnsafeStatePath(
                f'backup destination sidecar already exists: {sidecar}'
            )
        # 2. Prepare temporary file
        temporary = target.with_name(
            f'.{target.name}.backup-{os.getpid()}-{secrets.token_hex(6)}'
        )
        self._create_secure_file(temporary)
        backup_connection: sqlite3.Connection | None = None
        try:
            backup_connection = sqlite3.connect(temporary)
            with self._lock:
                self._connection.backup(backup_connection)
            backup_connection.commit()
            # 3. Publish backup file
            backup_connection.close()
            backup_connection = None
            os.chmod(temporary, 0o600, follow_symlinks=False)
            with temporary.open('rb') as backup_file:
                os.fsync(backup_file.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600, follow_symlinks=False)
            self._fsync_directory(target.parent)
            return target
        finally:
            # 4. Cleanup temporary file
            if backup_connection is not None:
                backup_connection.close()
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _configure_connection(self) -> None:
        """Configure database connection."""
        self._connection.execute('PRAGMA foreign_keys = ON')
        self._connection.execute('PRAGMA busy_timeout = 30000')
        self._connection.execute('PRAGMA synchronous = FULL')
        deadline = time.monotonic() + 30
        while True:
            try:
                mode = self._connection.execute(
                    'PRAGMA journal_mode = WAL'
                ).fetchone()[0]
                break
            except sqlite3.OperationalError as exc:
                if (
                    exc.sqlite_errorcode
                    not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or time.monotonic() >= deadline
                ):
                    raise
                # === WAL retry handling ===
                time.sleep(0.01)
        if str(mode).lower() != 'wal':
            raise OAuthStateError('SQLite could not enable WAL mode')

    def _migrate_legacy_once(self) -> None:
        """Perform one legacy migration."""
        # 1. Load migration state
        migration_name = 'legacy_clients_json_v1'
        source_bytes: bytes | None = None
        source_digest = ''
        records: tuple[MigrationRecord, ...] = ()
        cleanup_pending = False

        with self._transaction() as connection:
            marker = connection.execute(
                'SELECT * FROM migration_metadata WHERE name = ?',
                (migration_name,),
            ).fetchone()
            if marker is not None:
                cleanup_pending = bool(marker['cleanup_pending'])
                source_digest = str(marker['source_digest'])
            elif self.legacy_path is None or not os.path.lexists(
                self.legacy_path
            ):
                if self.approved_legacy_client_ids:
                    raise ValueError(
                        'approved legacy client IDs are absent from migration source'
                    )
                connection.execute(
                    'INSERT INTO migration_metadata '
                    '(name, source_digest, imported_count, completed_at, '
                    "cleanup_pending) VALUES (?, '', 0, ?, 0)",
                    (migration_name, self._clock()),
                )
                return
            else:
                self._assert_outside_downloads(self.legacy_path)
                self._validate_secure_file(
                    self.legacy_path, allow_missing=False
                )
                source_bytes = self.legacy_path.read_bytes()
                source_digest = hashlib.sha256(source_bytes).hexdigest()
                records = self._parse_legacy_records(source_bytes)
                imported_ids = {record.client_id for record in records}
                missing = self.approved_legacy_client_ids - imported_ids
                if missing:
                    joined = ', '.join(sorted(missing))
                    raise ValueError(
                        f'approved legacy client IDs absent from source: {joined}'
                    )
                # 2. Import legacy clients
                for record in records:
                    policy = (
                        LEGACY_FULL
                        if record.client_id in self.approved_legacy_client_ids
                        else REAUTHORIZATION_REQUIRED
                    )
                    connection.execute(
                        'INSERT INTO clients '
                        '(client_id, secret_verifier, policy, is_static, '
                        'client_name, created_at) VALUES (?, ?, ?, 0, ?, ?)',
                        (
                            record.client_id,
                            _secret_verifier('client', record.client_secret),
                            policy,
                            record.client_name,
                            record.created_at,
                        ),
                    )
                    connection.executemany(
                        'INSERT INTO client_redirect_uris '
                        '(client_id, redirect_uri) VALUES (?, ?)',
                        (
                            (record.client_id, redirect_uri)
                            for redirect_uri in record.redirect_uris
                        ),
                    )
                connection.execute(
                    'INSERT INTO migration_metadata '
                    '(name, source_digest, imported_count, completed_at, '
                    'cleanup_pending) VALUES (?, ?, ?, ?, 1)',
                    (
                        migration_name,
                        source_digest,
                        len(records),
                        self._clock(),
                    ),
                )
                cleanup_pending = True

        # 3. Finalize source cleanup
        if not cleanup_pending:
            return
        legacy_path = self.legacy_path
        if legacy_path is None:
            raise OAuthStateError(
                'legacy OAuth cleanup is pending but source path is unavailable'
            )
        with self._transaction() as connection:
            marker = connection.execute(
                'SELECT source_digest, cleanup_pending '
                'FROM migration_metadata WHERE name = ?',
                (migration_name,),
            ).fetchone()
            if marker is None or not bool(marker['cleanup_pending']):
                return
            expected_digest = str(marker['source_digest'])
            if os.path.lexists(legacy_path):
                self._validate_secure_file(legacy_path, allow_missing=False)
                current_digest = hashlib.sha256(
                    legacy_path.read_bytes()
                ).hexdigest()
                if current_digest != expected_digest:
                    raise OAuthStateError(
                        'legacy OAuth source changed after durable migration'
                    )
                try:
                    legacy_path.unlink()
                except FileNotFoundError:
                    pass
            self._fsync_directory(legacy_path.parent)
            connection.execute(
                'UPDATE migration_metadata SET cleanup_pending = 0 WHERE name = ?',
                (migration_name,),
            )

    def _parse_legacy_records(
        self, source_bytes: bytes
    ) -> tuple[MigrationRecord, ...]:
        """Parse legacy client records."""

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            """Reject duplicate fields."""
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(
                        f'duplicate key in legacy OAuth source: {key}'
                    )
                result[key] = value
            return result

        # Decode legacy source
        try:
            payload = json.loads(
                source_bytes.decode('utf-8'),
                object_pairs_hook=reject_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('malformed legacy OAuth source') from exc
        if not isinstance(payload, dict):
            raise ValueError('legacy OAuth source must contain an object')

        # Validate legacy records
        records: list[MigrationRecord] = []
        for client_id, raw in payload.items():
            if not isinstance(client_id, str) or not client_id:
                raise ValueError('legacy client ID must be a non-empty string')
            if not isinstance(raw, dict):
                raise ValueError(
                    f'legacy client {client_id!r} must be an object'
                )
            client_secret = raw.get('client_secret')
            redirects = raw.get('redirect_uris')
            if not isinstance(client_secret, str) or not client_secret:
                raise ValueError(
                    f'legacy client {client_id!r} has no valid client_secret'
                )
            if (
                not isinstance(redirects, list)
                or not redirects
                or not all(isinstance(uri, str) and uri for uri in redirects)
            ):
                raise ValueError(
                    f'legacy client {client_id!r} has invalid redirect_uris'
                )
            normalized_redirects = tuple(dict.fromkeys(redirects))
            if len(normalized_redirects) != len(redirects):
                raise ValueError(
                    f'legacy client {client_id!r} has duplicate redirect URIs'
                )
            created_at = raw.get('created_at', self._clock())
            if not isinstance(created_at, (int, float)):
                raise ValueError(
                    f'legacy client {client_id!r} has invalid created_at'
                )
            client_name = raw.get(
                'client_name', 'Google Workspace MCP Migrated Client'
            )
            if not isinstance(client_name, str):
                raise ValueError(
                    f'legacy client {client_id!r} has invalid client_name'
                )
            records.append(
                MigrationRecord(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uris=normalized_redirects,
                    client_name=client_name,
                    created_at=float(created_at),
                )
            )
        return tuple(records)

    def _authenticate_client_in_transaction(
        self,
        connection: sqlite3.Connection,
        client_id: str,
        client_secret: str,
    ) -> None:
        """Authenticate client transactionally."""
        row = connection.execute(
            'SELECT secret_verifier, is_static, revoked_at FROM clients '
            'WHERE client_id = ?',
            (client_id,),
        ).fetchone()
        if row is None or row['revoked_at'] is not None or not client_secret:
            raise InvalidClient('client authentication failed')
        if bool(row['is_static']):
            authenticator = self._static_client_authenticator
            valid = bool(
                authenticator is not None
                and authenticator(client_id, client_secret)
            )
        else:
            verifier = row['secret_verifier']
            valid = bool(
                verifier is not None
                and hmac.compare_digest(
                    bytes(verifier),
                    _secret_verifier('client', client_secret),
                )
            )
        if not valid:
            raise InvalidClient('client authentication failed')

    def _insert_access_token(
        self,
        connection: sqlite3.Connection,
        *,
        client_id: str,
        policy: str,
        capabilities: Sequence[str],
        resource: str,
        now: float,
    ) -> IssuedAccessToken:
        """Insert access token."""
        # 1. Normalize capabilities
        normalized_capabilities = _normalize_capabilities(capabilities)
        # 2. Allocate token identity
        while True:
            token_id = secrets.token_urlsafe(18)
            exists = connection.execute(
                'SELECT 1 FROM access_tokens WHERE token_id = ?', (token_id,)
            ).fetchone()
            if exists is None:
                break
        token_secret = secrets.token_urlsafe(32)
        expires_at = now + self.access_token_ttl_seconds
        # 3. Persist token record
        connection.execute(
            'INSERT INTO access_tokens '
            '(token_id, secret_verifier, client_id, policy, resource, '
            'issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                token_id,
                _secret_verifier('access-token', token_secret),
                client_id,
                policy,
                resource,
                now,
                expires_at,
            ),
        )
        connection.executemany(
            'INSERT INTO access_token_capabilities (token_id, capability) '
            'VALUES (?, ?)',
            ((token_id, capability) for capability in normalized_capabilities),
        )
        # 4. Build token metadata
        token = TokenMetadata(
            token_id=token_id,
            client_id=client_id,
            policy=policy,
            capabilities=normalized_capabilities,
            resource=resource,
            issued_at=now,
            expires_at=expires_at,
            revoked_at=None,
        )
        return IssuedAccessToken(
            access_token=f'v1.{token_id}.{token_secret}', token=token
        )

    def _insert_refresh_token(
        self,
        connection: sqlite3.Connection,
        *,
        client_id: str,
        family_id: str,
        access_token_id: str | None,
        policy: str,
        capabilities: Sequence[str],
        resource: str,
        now: float,
    ) -> tuple[str, RefreshTokenMetadata]:
        """Insert refresh token."""
        # 1. Normalize capabilities
        normalized_capabilities = _normalize_capabilities(capabilities)
        # 2. Allocate token identity
        while True:
            refresh_id = secrets.token_urlsafe(18)
            exists = connection.execute(
                'SELECT 1 FROM refresh_tokens WHERE refresh_id = ?',
                (refresh_id,),
            ).fetchone()
            if exists is None:
                break
        refresh_secret = secrets.token_urlsafe(32)
        expires_at = now + self.refresh_token_ttl_seconds
        # 3. Persist token record
        connection.execute(
            'INSERT INTO refresh_tokens '
            '(refresh_id, secret_verifier, family_id, client_id, '
            'access_token_id, policy, capabilities_json, resource, '
            'issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                refresh_id,
                _secret_verifier('refresh-token', refresh_secret),
                family_id,
                client_id,
                access_token_id,
                policy,
                json.dumps(list(normalized_capabilities)),
                resource,
                now,
                expires_at,
            ),
        )
        # 4. Build token metadata
        metadata = RefreshTokenMetadata(
            refresh_id=refresh_id,
            family_id=family_id,
            client_id=client_id,
            access_token_id=access_token_id,
            policy=policy,
            capabilities=normalized_capabilities,
            resource=resource,
            issued_at=now,
            expires_at=expires_at,
            consumed_at=None,
            revoked_at=None,
        )
        return f'r1.{refresh_id}.{refresh_secret}', metadata

    def _revoke_refresh_family(
        self,
        connection: sqlite3.Connection,
        family_id: str,
        now: float,
    ) -> None:
        """Revoke refresh token family."""
        connection.execute(
            'UPDATE access_tokens SET revoked_at = ? WHERE revoked_at IS NULL '
            'AND token_id IN (SELECT access_token_id FROM refresh_tokens '
            'WHERE family_id = ? AND access_token_id IS NOT NULL)',
            (now, family_id),
        )
        connection.execute(
            'UPDATE refresh_tokens SET revoked_at = ? '
            'WHERE family_id = ? AND revoked_at IS NULL',
            (now, family_id),
        )

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Open database transaction."""
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize_schema(self, *, initialize_owner: bool) -> None:
        """Prepare database schema."""
        with self._lock:
            version = int(
                self._connection.execute('PRAGMA user_version').fetchone()[0]
            )
            if version > SCHEMA_VERSION:
                raise OAuthStateError(
                    f'unsupported OAuth state schema version {version}'
                )
            script = f'BEGIN IMMEDIATE;\n{_SCHEMA}\n'
            try:
                self._connection.executescript(script)
                if initialize_owner:
                    self._connection.execute(
                        'INSERT INTO state_owner '
                        '(id, service_id, resource, created_at) '
                        'VALUES (1, ?, ?, ?)',
                        (self.service_id, self.resource, self._clock()),
                    )
                self._connection.execute(
                    f'PRAGMA user_version = {SCHEMA_VERSION}'
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _redirects_for(
        self, connection: sqlite3.Connection, client_id: str
    ) -> tuple[str, ...]:
        """Load client redirects."""
        rows = connection.execute(
            'SELECT redirect_uri FROM client_redirect_uris '
            'WHERE client_id = ? ORDER BY redirect_uri',
            (client_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _token_capabilities(
        self, connection: sqlite3.Connection, token_id: str
    ) -> tuple[str, ...]:
        """Load token capabilities."""
        rows = connection.execute(
            'SELECT capability FROM access_token_capabilities '
            'WHERE token_id = ? ORDER BY capability',
            (token_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _validate_state_targets(self) -> tuple[int, int] | None:
        """Validate state paths."""
        self._validate_secure_file(self.path, allow_missing=True)
        identity = (
            self._state_file_identity() if os.path.lexists(self.path) else None
        )
        sidecars = tuple(
            Path(f'{self.path}{suffix}') for suffix in ('-wal', '-shm')
        )
        for sidecar in sidecars:
            self._validate_secure_file(sidecar, allow_missing=True)
        return identity

    def _state_file_identity(self) -> tuple[int, int]:
        """Read state file identity."""
        metadata = self.path.lstat()
        return metadata.st_dev, metadata.st_ino

    def _assert_state_file_identity(
        self, expected_identity: tuple[int, int]
    ) -> None:
        """Verify stable file identity."""
        if self._state_file_identity() != expected_identity:
            raise UnsafeStatePath('OAuth state file changed during open')

    @contextlib.contextmanager
    def _state_initialization_lock(self) -> Iterator[None]:
        """Lock state initialization."""
        with _STATE_INITIALIZATION_LOCK:
            lock_path = Path(f'{self.path}.init-lock')
            if not os.path.lexists(lock_path):
                try:
                    self._create_secure_file(lock_path)
                except FileExistsError:
                    pass
            self._validate_secure_file(lock_path, allow_missing=False)
            flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(lock_path, flags)
            locked = False
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                yield
            finally:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _create_database_file(self) -> bool:
        """Create secure database file."""
        created = False
        if not os.path.lexists(self.path):
            try:
                self._create_secure_file(self.path)
                created = True
            except FileExistsError:
                pass
        self._validate_secure_file(self.path, allow_missing=False)
        return created

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Sync directory contents."""
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _secure_state_files(self) -> None:
        """Secure state files."""
        os.chmod(self.path, 0o600, follow_symlinks=False)
        for suffix in ('-wal', '-shm'):
            sidecar = Path(f'{self.path}{suffix}')
            if os.path.lexists(sidecar):
                self._validate_secure_file(sidecar, allow_missing=False)
                os.chmod(sidecar, 0o600, follow_symlinks=False)

    def _prepare_secure_directory(self, directory: Path) -> None:
        """Prepare secure directory."""
        directory = directory.absolute()
        if os.path.lexists(directory):
            metadata = directory.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeStatePath(
                    f'state directory is a symlink: {directory}'
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafeStatePath(
                    f'state directory is not a directory: {directory}'
                )
        else:
            parent = directory.parent
            if parent.resolve(strict=False) != parent.absolute():
                raise UnsafeStatePath(
                    f'state directory parent traverses a symlink: {parent}'
                )
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
        self._validate_owner_and_mode(metadata, directory, is_directory=True)
        os.chmod(directory, 0o700, follow_symlinks=False)

    def _validate_secure_file(
        self, path: Path, *, allow_missing: bool
    ) -> None:
        """Validate secure file."""
        if not os.path.lexists(path):
            if allow_missing:
                return
            raise UnsafeStatePath(f'required state file is missing: {path}')
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeStatePath(f'state file is a symlink: {path}')
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeStatePath(f'state path is not a regular file: {path}')
        self._validate_owner_and_mode(metadata, path, is_directory=False)

    def _validate_owner_and_mode(
        self, metadata: os.stat_result, path: Path, *, is_directory: bool
    ) -> None:
        """Validate path ownership."""
        if metadata.st_uid != os.getuid():
            raise UnsafeStatePath(f'state path has unsafe owner: {path}')
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o077:
            kind = 'directory' if is_directory else 'file'
            raise UnsafeStatePath(
                f'state {kind} has unsafe mode {mode:o}: {path}'
            )

    def _preflight_existing_state_owner(
        self, expected_identity: tuple[int, int]
    ) -> None:
        """Read owner without sidecars."""
        self._assert_state_file_identity(expected_identity)
        uri = f'{self.path.as_uri()}?mode=ro&immutable=1'
        with sqlite3.connect(uri, uri=True) as connection:
            owner_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'state_owner'"
            ).fetchone()
            row = None
            if owner_table is not None:
                try:
                    row = connection.execute(
                        'SELECT service_id, resource FROM state_owner WHERE id = 1'
                    ).fetchone()
                except sqlite3.OperationalError as exc:
                    if 'no such table' not in str(exc):
                        raise
        self._assert_state_file_identity(expected_identity)
        if row is None:
            raise UnsafeStatePath('existing OAuth state has no owner metadata')
        self._validate_state_owner_values(str(row[0]), str(row[1]))

    def _assert_connected_state_owner(self) -> None:
        """Recheck connected state owner."""
        try:
            row = self._connection.execute(
                'SELECT service_id, resource FROM state_owner WHERE id = 1'
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            row = None
        if row is None:
            raise UnsafeStatePath('existing OAuth state has no owner metadata')
        self._validate_state_owner_values(
            str(row['service_id']), str(row['resource'])
        )

    def _validate_state_owner_values(
        self, service_id: str, resource: str
    ) -> None:
        """Validate persisted owner values."""
        if service_id != self.service_id or resource != self.resource:
            raise UnsafeStatePath('OAuth state owner mismatch')

    def _assert_outside_downloads(self, path: Path) -> None:
        """Validate external path."""
        resolved_path = path.resolve(strict=False)
        resolved_downloads = self.download_path.resolve(strict=False)
        try:
            common = Path(
                os.path.commonpath((resolved_path, resolved_downloads))
            )
        except ValueError:
            return
        if common == resolved_downloads:
            raise UnsafeStatePath(
                f'OAuth state must remain outside the download directory: {path}'
            )

    @staticmethod
    def _create_secure_file(path: Path) -> None:
        """Create secure file."""
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    @staticmethod
    def _normalize_redirects(redirect_uris: Sequence[str]) -> tuple[str, ...]:
        """Normalize redirect URIs."""
        redirects = tuple(dict.fromkeys(redirect_uris))
        if not all(isinstance(uri, str) and uri for uri in redirects):
            raise ValueError('redirect URIs must be non-empty strings')
        return redirects

    @staticmethod
    def _validate_policy(policy: str) -> None:
        """Validate OAuth policy."""
        if policy not in {
            MCP_READONLY_V1,
            LEGACY_FULL,
            REAUTHORIZATION_REQUIRED,
        }:
            raise ValueError(f'unknown OAuth policy: {policy}')


def _secret_verifier(kind: str, secret: str) -> bytes:
    """Derive secret verifier."""
    digest = hashlib.sha256()
    digest.update(b'google-workspace-mcp/oauth-state/v1\0')
    digest.update(kind.encode('ascii'))
    digest.update(b'\0')
    digest.update(secret.encode('utf-8'))
    return digest.digest()


def _normalize_capabilities(capabilities: Iterable[str]) -> tuple[str, ...]:
    """Normalize token capabilities."""
    result = tuple(sorted(set(capabilities)))
    if not all(
        isinstance(capability, str) and capability for capability in result
    ):
        raise ValueError('capabilities must be non-empty strings')
    return result


def _parse_access_token(access_token: str) -> tuple[str, str] | None:
    """Parse access token."""
    if not isinstance(access_token, str):
        return None
    parts = access_token.split('.')
    if len(parts) != 3 or parts[0] != 'v1' or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _parse_refresh_token(refresh_token: str) -> tuple[str, str] | None:
    """Parse refresh token."""
    if not isinstance(refresh_token, str):
        return None
    parts = refresh_token.split('.')
    if len(parts) != 3 or parts[0] != 'r1' or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _capabilities_for_policy(
    policy: str, readonly_capabilities: tuple[str, ...]
) -> tuple[str, ...]:
    """Resolve policy capabilities."""
    if policy == MCP_READONLY_V1:
        return readonly_capabilities
    if policy in {LEGACY_FULL, REAUTHORIZATION_REQUIRED}:
        return ()
    raise ValueError(f'unknown OAuth policy: {policy}')


def _client_from_row(
    row: sqlite3.Row,
    redirect_uris: tuple[str, ...],
    readonly_capabilities: tuple[str, ...] = (),
) -> ClientMetadata:
    """Build client metadata."""
    return ClientMetadata(
        client_id=str(row['client_id']),
        redirect_uris=redirect_uris,
        policy=str(row['policy']),
        client_name=str(row['client_name']),
        created_at=float(row['created_at']),
        last_authorized_at=(
            float(row['last_authorized_at'])
            if row['last_authorized_at'] is not None
            else None
        ),
        revoked_at=(
            float(row['revoked_at']) if row['revoked_at'] is not None else None
        ),
        is_static=bool(row['is_static']),
        capabilities=_capabilities_for_policy(
            str(row['policy']), readonly_capabilities
        ),
    )


def _refresh_from_row(row: sqlite3.Row) -> RefreshTokenMetadata:
    """Build refresh metadata."""
    return RefreshTokenMetadata(
        refresh_id=str(row['refresh_id']),
        family_id=str(row['family_id']),
        client_id=str(row['client_id']),
        access_token_id=(
            str(row['access_token_id'])
            if row['access_token_id'] is not None
            else None
        ),
        policy=str(row['policy']),
        capabilities=tuple(json.loads(row['capabilities_json'])),
        resource=str(row['resource']),
        issued_at=float(row['issued_at']),
        expires_at=float(row['expires_at']),
        consumed_at=(
            float(row['consumed_at'])
            if row['consumed_at'] is not None
            else None
        ),
        revoked_at=(
            float(row['revoked_at']) if row['revoked_at'] is not None else None
        ),
    )


def _token_from_row(
    row: sqlite3.Row, capabilities: tuple[str, ...]
) -> TokenMetadata:
    """Build token metadata."""
    return TokenMetadata(
        token_id=str(row['token_id']),
        client_id=str(row['client_id']),
        policy=str(row['policy']),
        capabilities=capabilities,
        resource=str(row['resource']),
        issued_at=float(row['issued_at']),
        expires_at=float(row['expires_at']),
        revoked_at=(
            float(row['revoked_at']) if row['revoked_at'] is not None else None
        ),
    )
