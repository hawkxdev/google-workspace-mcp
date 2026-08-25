"""Define Calendar service errors."""


class CalendarError(Exception):
    """Base Calendar service error."""


class CalendarInputError(CalendarError):
    """Reject invalid Calendar input."""


class CalendarProviderError(CalendarError):
    """Report safe provider failure."""


class CalendarConflictError(CalendarError):
    """Report stale Calendar state."""
