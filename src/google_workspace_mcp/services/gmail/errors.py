"""Define Gmail service errors."""


class GmailError(Exception):
    """Base Gmail service error."""


class GmailInputError(GmailError):
    """Reject invalid Gmail input."""


class GmailPayloadError(GmailError):
    """Reject malformed Gmail payload."""


class GmailAttachmentError(GmailError):
    """Reject unsafe Gmail attachment."""


class GmailProviderError(GmailError):
    """Report safe provider failure."""
