"""Bound Gmail provider transport."""

from __future__ import annotations

import logging
from typing import Any

import httplib2
import requests

from google_workspace_mcp.common.deadline import (
    OperationDeadline,
    current_deadline,
)
from google_workspace_mcp.google_auth import GoogleCredentials


class ReadDiagnosticFilter(logging.Filter):
    """Suppress contextual provider payloads."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Preserve unrelated provider diagnostics."""
        return current_deadline() is None


_READ_FILTER = ReadDiagnosticFilter()
for _logger_name in (
    'googleapiclient.http',
    'googleapiclient.discovery',
    'google.auth.transport.requests',
    'google.oauth2._client',
    'urllib3.connectionpool',
    'urllib3.util.retry',
):
    logging.getLogger(_logger_name).addFilter(_READ_FILTER)


class DeadlineHttp:
    """Check each provider transmission."""

    def __init__(
        self, credentials: GoogleCredentials, deadline: OperationDeadline
    ) -> None:
        """Initialize bounded HTTP transport."""
        self.credentials = credentials.to_google_credentials()
        self.deadline = deadline
        self.session = requests.Session()

    def request(
        self,
        uri: str,
        method: str = 'GET',
        body: Any = None,
        headers: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Bound provider socket waits."""
        timeout = min(self.deadline.attempt_seconds, self.deadline.remaining())
        request_headers = dict(headers or {})
        self.credentials.apply(request_headers)
        try:
            response = self.session.request(
                method,
                uri,
                data=body,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise TimeoutError('Gmail provider attempt timed out') from None
        except requests.RequestException:
            raise ConnectionError('Gmail provider transport failed') from None
        self.deadline.remaining()
        return (
            httplib2.Response(
                {**response.headers, 'status': str(response.status_code)}
            ),
            response.content,
        )

    def close(self) -> None:
        """Close owned provider session."""
        self.session.close()


def authorized_read_http(
    credentials: GoogleCredentials,
    deadline: OperationDeadline,
) -> DeadlineHttp:
    """Build bounded authorized transport."""
    transport = DeadlineHttp(credentials, deadline)
    deadline.cleanups.append(transport.close)
    return transport
