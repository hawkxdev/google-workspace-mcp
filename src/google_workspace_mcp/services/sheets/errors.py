"""Define Sheets service errors."""

from __future__ import annotations


class SheetsError(Exception):
    """Base Sheets service error."""


class SheetsInputError(SheetsError):
    """Reject invalid Sheets input."""


class SheetsNotFoundError(SheetsError):
    """Report missing Sheets resource."""


class SheetsProviderError(SheetsError):
    """Report safe provider failure."""


class SheetsRateLimitError(SheetsError):
    """Report Sheets rate limit."""


class SheetsScopeError(SheetsError):
    """Reject missing write scope."""
