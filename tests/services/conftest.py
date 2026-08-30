"""Provide Gmail service fakes."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from google_workspace_mcp.common.config import ServiceConfig
from google_workspace_mcp.google_auth import GoogleCredentials


class FakeRequest:
    """Record provider request execution."""

    def __init__(
        self, value: Any = None, error: Exception | None = None
    ) -> None:
        """Initialize fake provider request."""
        self.value = value
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0) -> Any:
        """Execute configured provider response."""
        self.retries.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.value


class FakeCollection:
    """Record provider collection calls."""

    def __init__(self) -> None:
        """Initialize fake provider collection."""
        self.calls: list[tuple[str, dict[str, Any], FakeRequest]] = []
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.attachments_collection: FakeCollection | None = None

    def queue(self, method: str, *values: Any) -> None:
        """Queue provider method responses."""
        self.responses[method].extend(values)

    def _call(self, method: str, kwargs: dict[str, Any]) -> FakeRequest:
        """Create recorded provider request."""
        value = self.responses[method].popleft()
        request = (
            value if isinstance(value, FakeRequest) else FakeRequest(value)
        )
        self.calls.append((method, kwargs, request))
        return request

    def list(self, **kwargs: Any) -> FakeRequest:
        """Record list request."""
        return self._call('list', kwargs)

    def get(self, **kwargs: Any) -> FakeRequest:
        """Record get request."""
        return self._call('get', kwargs)

    def modify(self, **kwargs: Any) -> FakeRequest:
        """Record modify request."""
        return self._call('modify', kwargs)

    def create(self, **kwargs: Any) -> FakeRequest:
        """Record create request."""
        return self._call('create', kwargs)

    def update(self, **kwargs: Any) -> FakeRequest:
        """Record update request."""
        return self._call('update', kwargs)

    def delete(self, **kwargs: Any) -> FakeRequest:
        """Record delete request."""
        return self._call('delete', kwargs)

    def send(self, **kwargs: Any) -> FakeRequest:
        """Record send request."""
        return self._call('send', kwargs)

    def attachments(self) -> FakeCollection:
        """Return attachment collection."""
        assert self.attachments_collection is not None
        return self.attachments_collection


class FakeUsers:
    """Expose fake Gmail collections."""

    def __init__(self) -> None:
        """Initialize Gmail collections."""
        self.message_collection = FakeCollection()
        self.thread_collection = FakeCollection()
        self.label_collection = FakeCollection()
        self.draft_collection = FakeCollection()
        self.attachment_collection = FakeCollection()
        self.message_collection.attachments_collection = (
            self.attachment_collection
        )

    def messages(self) -> FakeCollection:
        """Return message collection."""
        return self.message_collection

    def threads(self) -> FakeCollection:
        """Return thread collection."""
        return self.thread_collection

    def labels(self) -> FakeCollection:
        """Return label collection."""
        return self.label_collection

    def drafts(self) -> FakeCollection:
        """Return draft collection."""
        return self.draft_collection


class FakeGmailService:
    """Expose fake Gmail users."""

    def __init__(self) -> None:
        """Initialize fake Gmail service."""
        self.users_resource = FakeUsers()

    def users(self) -> FakeUsers:
        """Return users resource."""
        return self.users_resource


class FakeCredentialStore:
    """Return synthetic Google credentials."""

    def __init__(self) -> None:
        """Initialize fake credential store."""
        self.calls = 0
        self.credentials = GoogleCredentials(
            token='provider-token',
            scopes=('https://www.googleapis.com/auth/gmail.modify',),
        )

    def refresh(self) -> GoogleCredentials:
        """Return refreshed fake credentials."""
        self.calls += 1
        return self.credentials


@pytest.fixture
def gmail_service() -> FakeGmailService:
    """Create fake Gmail service."""
    return FakeGmailService()


@pytest.fixture
def credential_store() -> FakeCredentialStore:
    """Create fake credential store."""
    return FakeCredentialStore()


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    """Create isolated Gmail config."""
    root = tmp_path / 'gmail'
    root.mkdir(mode=0o700)
    downloads = root / 'downloads'
    downloads.mkdir(mode=0o700)
    return ServiceConfig(
        service_id='gmail',
        public_url='https://mcp.example.test/gmail',
        mcp_path='/gmail/mcp',
        host='127.0.0.1',
        port=8431,
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


ServiceBuilder = Callable[[GoogleCredentials], FakeGmailService]
