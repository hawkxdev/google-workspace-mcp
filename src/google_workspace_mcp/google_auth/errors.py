"""Define Google authentication errors."""


class GoogleAuthError(RuntimeError):
    """Base Google authentication error."""


class TokenRevokedError(GoogleAuthError):
    """Report revoked refresh token."""


class ScopeMismatchError(GoogleAuthError):
    """Report missing required scopes."""


class UnsafeCredentialPath(GoogleAuthError):
    """Report unsafe credential path."""
