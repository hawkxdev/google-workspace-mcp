"""Model Google authorized credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.oauth2.credentials import Credentials as OAuth2Credentials


def _parse_expiry(value: Any) -> datetime | None:
    """Parse stored expiry value."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('invalid credential expiry')
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class GoogleCredentials:
    """Store Google authorized credentials."""

    token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_uri: str = 'https://oauth2.googleapis.com/token'
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()
    expiry: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoogleCredentials:
        """Build credentials from mapping."""
        raw_scopes = data.get('scopes') or ()
        if isinstance(raw_scopes, str):
            scopes = tuple(raw_scopes.split())
        elif isinstance(raw_scopes, (list, tuple)):
            scopes = tuple(str(scope) for scope in raw_scopes)
        else:
            raise ValueError('invalid credential scopes')
        return cls(
            token=data.get('token'),
            refresh_token=data.get('refresh_token'),
            token_uri=data.get('token_uri')
            or 'https://oauth2.googleapis.com/token',
            client_id=data.get('client_id'),
            client_secret=data.get('client_secret'),
            scopes=scopes,
            expiry=_parse_expiry(data.get('expiry')),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize credentials for storage."""
        expiry = None
        if self.expiry is not None:
            expiry = self.expiry.astimezone(UTC).isoformat()
        return {
            'token': self.token,
            'refresh_token': self.refresh_token,
            'token_uri': self.token_uri,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scopes': list(self.scopes),
            'expiry': expiry,
        }

    def to_google_credentials(self) -> OAuth2Credentials:
        """Build library credential object."""
        expiry = None
        if self.expiry is not None:
            expiry = self.expiry.astimezone(UTC).replace(tzinfo=None)
        return OAuth2Credentials(
            token=self.token,
            refresh_token=self.refresh_token,
            token_uri=self.token_uri,
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=list(self.scopes) if self.scopes else None,
            expiry=expiry,
        )

    @classmethod
    def from_google_credentials(
        cls,
        credentials: OAuth2Credentials,
        *,
        fallback_scopes: tuple[str, ...] = (),
    ) -> GoogleCredentials:
        """Build from library credentials."""
        if credentials.granted_scopes is not None:
            scopes = tuple(credentials.granted_scopes)
        elif credentials.scopes is not None:
            scopes = tuple(credentials.scopes)
        else:
            scopes = fallback_scopes
        expiry = credentials.expiry
        if expiry is not None:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            else:
                expiry = expiry.astimezone(UTC)
        return cls(
            token=credentials.token,
            refresh_token=credentials.refresh_token,
            token_uri=credentials.token_uri
            or 'https://oauth2.googleapis.com/token',
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            scopes=scopes,
            expiry=expiry,
        )
