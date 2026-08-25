"""Define Sheets service limits."""

SHEETS_SCOPES = (
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file',
)
MAX_SHEETS_RANGES = 20
MAX_SHEETS_CELLS = 10_000
MAX_SHEETS_GRID_CELLS = 10_000_000
MAX_SHEETS_PAYLOAD_BYTES = 1_048_576
MAX_SHEETS_A1_CHARS = 512
MAX_SHEETS_TITLE_CHARS = 100
MAX_SHEETS_TEXT_CHARS = 50_000
REQUEST_RETRIES = 3
