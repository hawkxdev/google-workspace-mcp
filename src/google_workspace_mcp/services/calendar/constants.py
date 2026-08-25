"""Define Calendar service limits."""

CALENDAR_SCOPES = (
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
    'https://www.googleapis.com/auth/calendar.freebusy',
)
DEFAULT_PAGE_SIZE = 10
MAX_EVENT_PAGE_SIZE = 50
MAX_CALENDAR_PAGE_SIZE = 250
MAX_BATCH_OPERATIONS = 20
MAX_FREEBUSY_CALENDARS = 50
MAX_ATTENDEES = 100
MAX_REMINDERS = 5
MAX_RECURRENCE_LINES = 10
MAX_TEXT_CHARS = 4_000
MAX_ID_CHARS = 256
MAX_WINDOW_DAYS = 366
REQUEST_RETRIES = 2
USER_TIMEZONE = 'UTC'
