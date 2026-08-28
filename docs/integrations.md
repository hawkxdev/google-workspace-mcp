# Google Workspace Integrations

Hawkx Workspace MCP exposes 55 tools across five isolated Google Workspace services.

Each service uses a separate Google credential and exposes only its own tool registry.

## Scope Ownership

| Service | Google OAuth scopes |
|---|---|
| Gmail | `gmail.modify` |
| Calendar | `calendar.events`, `calendar.calendarlist.readonly`, `calendar.freebusy` |
| Drive | `drive.readonly`, `drive.file` |
| Sheets | `spreadsheets`, `drive.file` |
| Docs | `documents`, `drive.file` |

The repeated `drive.file` scope is part of three independent grants. It does not create a shared token.

See [Google Cloud and OAuth Setup](google-cloud-setup.md) for full scope URLs and Google classifications.

## Gmail

Gmail provides 18 tools.

### Read tools

| Tool | Purpose |
|---|---|
| `gmail_search_messages` | Search messages and return bounded summaries |
| `gmail_search_threads` | Search threads and return bounded summaries |
| `gmail_get_message` | Read one normalized message |
| `gmail_get_thread` | Read one normalized thread |
| `gmail_list_labels` | List available labels |
| `gmail_list_drafts` | List drafts with cursor pagination |
| `gmail_get_draft` | Read one draft |

### Label and attachment tools

| Tool | Purpose |
|---|---|
| `gmail_modify_labels` | Add or remove labels from one message or thread |
| `gmail_archive` | Archive one message or thread |
| `gmail_mark_read` | Mark one message or thread as read |
| `gmail_mark_unread` | Mark one message or thread as unread |
| `gmail_download_attachment` | Store one attachment in managed service storage |

### Draft and send tools

| Tool | Purpose |
|---|---|
| `gmail_create_draft` | Create a plain-text draft |
| `gmail_update_draft` | Replace a plain-text draft |
| `gmail_delete_draft` | Permanently delete a draft |
| `gmail_send_draft` | Send an existing draft |
| `gmail_send_message` | Send a plain-text message |
| `gmail_reply` | Reply to the original author of a message |

### Gmail bounds

| Limit | Value |
|---|---:|
| Search page size | 20 |
| Message body characters | 20,000 |
| Thread messages returned | 50 |
| Attachment size | 25 MiB |
| Labels returned | 1,000 |

The service does not request the full-mail scope and does not support irreversible message deletion outside normal Gmail label workflows.

## Calendar

Calendar provides 9 tools.

| Tool | Purpose |
|---|---|
| `calendar_list_calendars` | List accessible calendars |
| `calendar_search_events` | Search expanded events in a bounded time window |
| `calendar_get_event` | Read one normalized event |
| `calendar_list_event_instances` | List recurring-event instances |
| `calendar_get_freebusy` | Read availability for selected calendars |
| `calendar_create_event` | Create a timed or all-day event |
| `calendar_update_event` | Update one occurrence, a series, or future instances |
| `calendar_delete_event` | Delete one occurrence, a series, or future instances |
| `calendar_batch_mutate_events` | Execute bounded event mutations with per-item results |

### Calendar bounds

| Limit | Value |
|---|---:|
| Calendar page size | 250 |
| Event page size | 50 |
| Search window | 366 days |
| Free/busy calendars | 50 |
| Batch mutations | 20 |
| Event attendees | 100 |
| Event reminders | 5 |
| Recurrence lines | 10 |
| Text field length | 4,000 characters |

Calendar event writes use provider version information when available. A stale event state produces a conflict instead of silently overwriting newer data.

The service does not manage calendars, sharing permissions, or access-control lists.

## Drive

Drive provides 10 tools.

| Tool | Purpose |
|---|---|
| `drive_search_files` | Search files with structured filters |
| `drive_get_file` | Read metadata for one file |
| `drive_list_folder` | List folder contents |
| `drive_download_file` | Store one binary file in managed storage |
| `drive_export_file` | Export a Google Workspace file into managed storage |
| `drive_create_folder` | Create a folder |
| `drive_upload_file` | Upload one managed local file |
| `drive_update_file` | Update metadata or content with version preflight |
| `drive_move_file` | Move a file with version preflight |
| `drive_copy_file` | Create an app-owned copy |

### Drive bounds

| Limit | Value |
|---|---:|
| Search results per page | 50 |
| Binary download | 25 MiB |
| Google Workspace export | 10 MiB |
| Parent folders per file | 100 |

`drive.readonly` permits discovery and reading of existing files. `drive.file` limits writes to files created by or explicitly opened with the application.

Download and export results use the Drive managed-file boundary. They are never written to the credential or OAuth state directory.

## Sheets

Sheets provides 11 tools.

### Read tools

| Tool | Purpose |
|---|---|
| `sheets_get_spreadsheet` | Read spreadsheet and sheet metadata |
| `sheets_read_range` | Read one A1 range |
| `sheets_batch_read_ranges` | Read multiple A1 ranges |

### Value tools

| Tool | Purpose |
|---|---|
| `sheets_update_range` | Write values to one A1 range |
| `sheets_append_rows` | Append rows to a table |
| `sheets_batch_update_ranges` | Write multiple ranges in one provider request |
| `sheets_clear_ranges` | Clear values and formulas from ranges |

### Structure tools

| Tool | Purpose |
|---|---|
| `sheets_create_spreadsheet` | Create a spreadsheet |
| `sheets_add_sheet` | Add a sheet tab |
| `sheets_rename_sheet` | Rename a sheet tab |
| `sheets_copy_sheet` | Copy a sheet within or between spreadsheets |

### Sheets bounds

| Limit | Value |
|---|---:|
| Ranges per batch | 20 |
| Cells per operation | 10,000 |
| Grid cells per spreadsheet response | 10,000,000 |
| Request payload | 1 MiB |
| A1 range length | 512 characters |
| Text value length | 50,000 characters |

Sheets does not provide optimistic concurrency. Concurrent writes follow Google Sheets last-write-wins behavior.

Callers choose explicit value render, date/time render, and raw or user-entered input modes where the tool supports them.

## Docs

Docs provides 7 tools.

| Tool | Purpose |
|---|---|
| `docs_get_document` | Read title, revision, and recursive tab metadata |
| `docs_read_content` | Read bounded typed content from one explicit tab |
| `docs_create_document` | Create an empty document |
| `docs_insert_text` | Insert text at one UTF-16 index |
| `docs_replace_text` | Replace a single-line literal inside one tab |
| `docs_delete_range` | Delete one half-open UTF-16 range |
| `docs_batch_update` | Apply up to twenty typed operations to one tab |

### Docs addressing

- Text indices use UTF-16 code units.
- Ranges are half-open.
- Reads and mutations address one explicit tab.
- Every mutation except document creation requires the latest known document revision.
- The mandatory final newline cannot be deleted.
- An index cannot split a surrogate pair.
- Raw provider requests and raw field masks are rejected.

### Docs read bounds

| Limit | Value |
|---|---:|
| Blocks per response | 100 |
| Text characters per response | 20,000 |
| Structural nodes | 2,000 |
| Document tabs | 200 |
| Tab nesting depth | 10 |
| Structural block depth | 10 |

A truncated read returns `next_start_index`. Continue from that UTF-16 index to resume inside the same block.

Unsupported structures are reported explicitly instead of being silently flattened. An oversized supported structure is distinguished from a malformed Google response.

### Docs mutation rules

- A batch contains at most 20 typed operations.
- Batch operations run in caller order.
- Every index is validated against the supplied revision.
- A batch contains at most one literal replacement.
- A replacement cannot be combined with index-shifting operations.
- Writes are not retried automatically after an uncertain provider outcome.

When a write outcome is unknown, reread the document and compare its revision before deciding whether another mutation is safe.

## Managed Files

Gmail attachments, Drive downloads, and Drive exports use per-service managed directories.

Managed files have:

- safe service-generated names;
- configured size limits;
- collision detection;
- atomic publication;
- SHA-256 metadata.

A managed path cannot contain OAuth state, Google credentials, audit logs, or backups.

## Pagination and Continuation

Gmail, Calendar, and Drive list operations accept bounded page sizes and optional provider page tokens. Responses return `next_page_token` when more items are available.

Docs uses UTF-16 continuation rather than provider page tokens. A bounded content response returns `truncated` and `next_start_index`.

Clients control continuation. The services do not fetch an unbounded result set automatically.

## Error Boundaries

Provider and network errors are translated into service-specific safe errors.

| Service | Error groups |
|---|---|
| Gmail | input, payload, attachment, provider |
| Calendar | input, provider, conflict |
| Drive | input, managed file, provider, conflict, scope |
| Sheets | input, not found, provider, rate limit, scope |
| Docs | input, not found, provider, rate limit, conflict, scope, unsupported structure, indeterminate write |

Raw provider exception bodies, request URLs, credentials, and unbounded response mappings are not returned to MCP clients.

## Intentionally Unsupported Behavior

- Full Gmail mailbox access and irreversible message deletion
- Calendar administration and sharing-permission management
- Arbitrary Drive writes outside `drive.file`
- Arbitrary Google request payloads or field masks
- Automatic retries for writes with uncertain outcomes
- Unbounded list or document-content reads
