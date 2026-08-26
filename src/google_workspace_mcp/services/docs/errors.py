"""Define Docs service errors."""

from __future__ import annotations


class DocsError(Exception):
    """Base Docs service error."""


class DocsInputError(DocsError):
    """Reject invalid Docs input."""


class DocsNotFoundError(DocsError):
    """Report missing Docs resource."""


class DocsProviderError(DocsError):
    """Report safe provider failure."""


class DocsRateLimitError(DocsError):
    """Report Docs rate limit."""


class DocsConflictError(DocsError):
    """Report stale document revision."""


class DocsScopeError(DocsError):
    """Reject missing write scope."""


class DocsUnsupportedError(DocsError):
    """Reject unsupported document structure."""
