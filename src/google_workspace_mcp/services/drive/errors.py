"""Define Drive service errors."""

from __future__ import annotations


class DriveError(Exception):
    """Base Drive service error."""


class DriveInputError(DriveError):
    """Reject invalid Drive input."""


class DriveManagedFileError(DriveError):
    """Reject managed file failure."""


class DriveProviderError(DriveError):
    """Report safe provider failure."""


class DriveConflictError(DriveError):
    """Report stale Drive state."""


class DriveScopeError(DriveError):
    """Reject missing write scope."""
