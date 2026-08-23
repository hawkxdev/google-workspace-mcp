"""Workspace service factories."""

from .calendar.factory import create_calendar_app
from .docs.factory import create_docs_app
from .drive.factory import create_drive_app
from .gmail.factory import create_gmail_app
from .sheets.factory import create_sheets_app

__all__ = [
    'create_calendar_app',
    'create_docs_app',
    'create_drive_app',
    'create_gmail_app',
    'create_sheets_app',
]
