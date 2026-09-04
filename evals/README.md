# Managed synthetic fixtures for Stage 12

## Purpose and public boundary

Fixture version `stage12-v1` defines fully synthetic objects for the upcoming 50 read-only evaluation tasks: ten each for Gmail, Calendar, Drive, Sheets, and Docs. This public directory holds only logical references, opaque markers, synthetic values, and the shape of future requests. It contains no Google IDs, no account address, no user content, no tokens, and no credential-reference values.

The task XML files appear after live seeding and a single readiness check. Until the registry reaches `ready`, `require_ready_for_xml()` refuses to allow authoring them.

## Version and logical references

A logical reference is stable within one fixture version. It is not a Google ID and resolves only through the private registry of the same version.

| Service | Logical reference | Object |
|---|---|---|
| Gmail | `gmail_draft_cobalt` | Draft |
| Gmail | `gmail_draft_message_cobalt` | Message inside the draft, not the draft ID |
| Gmail | `gmail_message_alpha_root` | Root message of thread alpha |
| Gmail | `gmail_message_alpha_reply` | Reply in thread alpha |
| Gmail | `gmail_thread_alpha` | Thread of two messages |
| Gmail | `gmail_delivery_alpha_root` | Confirmed delivery of the alpha root message |
| Gmail | `gmail_delivery_alpha_reply` | Confirmed delivery of the alpha reply |
| Gmail | `gmail_message_beta_root` | Sole message of thread beta |
| Gmail | `gmail_thread_beta` | Thread of one message |
| Gmail | `gmail_delivery_beta_root` | Confirmed delivery of the beta message |
| Calendar | `calendar_event_timed` | Timed event |
| Calendar | `calendar_event_all_day` | All-day event |
| Calendar | `calendar_event_recurring` | Series of three events |
| Drive | `drive_fixture_folder` | Dedicated synthetic folder |
| Drive | `drive_note_file` | Synthetic text file |
| Drive | `drive_ledger_file` | Synthetic CSV file |
| Sheets | `sheets_primary` | Sole synthetic spreadsheet of the suite |
| Sheets | `sheets_inputs_tab` | Sheet `Inputs` |
| Sheets | `sheets_summary_tab` | Sheet `Summary` |
| Docs | `docs_primary` | Sole synthetic document of the suite |
| Docs | `docs_primary_tab` | Primary tab of the document |

## The private `bindings.json`

The owner creates the registry `private/evals/bindings.json` before the first application: the file is written with mode `0600` inside a `0700` directory and carries the fixture version, state `planned`, two private values, and five credential references. The module intentionally provides no creation subcommand: these values come from the owner rather than being computed, so `apply` only reads a prepared registry and extends it as it goes. Loading rejects symlinks, special files, a foreign owner, a different mode, unknown fields, and an incompatible version. The fields `owner_email` and `calendar_primary_id` are private values and are masked by Pydantic on serialization. The `credentials.*.reference` fields hold references to separate user OAuth2 files only, and never a token or a client secret.

```json
{
  "fixture_version": "stage12-v1",
  "state": "planned",
  "owner_email": "<private-owner-address>",
  "calendar_primary_id": "<private-calendar-id>",
  "credentials": {
    "gmail": {"service": "gmail", "kind": "oauth_user", "reference": "oauth/gmail.json"},
    "calendar": {"service": "calendar", "kind": "oauth_user", "reference": "oauth/calendar.json"},
    "drive": {"service": "drive", "kind": "oauth_user", "reference": "oauth/drive.json"},
    "sheets": {"service": "sheets", "kind": "oauth_user", "reference": "oauth/sheets.json"},
    "docs": {"service": "docs", "kind": "oauth_user", "reference": "oauth/docs.json"}
  },
  "objects": {},
  "applied_operations": []
}
```

`planned` means seeding has not finished. After every successful request the write gate atomically appends the operation name to `applied_operations` and records the returned identifiers in `objects`: the write goes through a temporary file in the same directory, `fsync`, `os.replace`, and a directory `fsync`, so an interrupted process never leaves the registry unreadable. `applied` means all 14 write operations are registered but readiness is not yet confirmed. `ready` is permitted only after a complete read pass over every logical reference.

If the registry already holds some objects or operations, re-application is refused. A fresh preview then lists every remaining operation, marks a partially bound composite result as `blocked_partial_output`, and requires the owner to review the registry by hand. No account-wide search is performed to guess state. Fixture cleanup and deletion are out of scope for Stage 12.

## Full write preview

The command builds real `google-api-python-client` request objects and reads their `method`, `uri`, and `body` without calling `.execute()`:

```bash
cd app
uv run --no-sync python -m google_workspace_mcp.evals preview
```

To review the remainder against an existing private registry, pass `--bindings`:

```bash
uv run --no-sync python -m google_workspace_mcp.evals preview --bindings ../private/evals/bindings.json
```

The preview covers 14 operations: four Gmail, three Calendar, three Drive, two Sheets, and two Docs. The owner address in Gmail is replaced by the reference `bindings.owner_email`, the primary calendar identifier by the reference `bindings.calendar_primary_id`, and dependencies between requests appear as logical references.

Application runs through the `apply` subcommand of the same module. All of its arguments are mandatory and none has a default:

```bash
cd app
uv run --no-sync python -m google_workspace_mcp.evals apply \
  --bindings ../private/evals/bindings.json \
  --credentials-dir ../private/google-tokens \
  --fixture-version stage12-v1 \
  --preview-digest <digest from the preview_digest field of the shown preview> \
  --acknowledge-writes
```

The version and the SHA-256 digest are verified against the public anonymous preview before any credential is read and before the first request: a mismatch touches the transport zero times and leaves the registry unchanged. Seeding credentials are read as `<credentials-dir>/<service>.json` for the five services; the directory is checked for ownership and mode `0700`, and each file for ownership, regular type, absence of a symlink, and mode `0600`. The directory is named by an explicit argument because an implicit path to credentials that can write to a live account is unacceptable; the `credentials.*.reference` field plays no part in seeding and describes the future client-side storage of the evaluation run.

The 14 requests execute one at a time in registry order with zero automatic retries. Before each execution the logical placeholders and private values are resolved: the owner address replaces the synthetic one in the MIME payload, and the primary calendar identifier along with identifiers of previously created objects are taken from the registry. A request holding an unresolved placeholder never reaches the network. The first error stops the application and returns the operation identifier alone, without request content, path, or the original exception.

A service account is rejected for seeding because a personal Google account grants it no file-ownership quota.

The evaluation run process neither accepts nor receives full-access tokens. After seeding it uses five separate `mcp_readonly_v1` client tokens and the exact 7/5/3/3/2 tool registries. Token values never appear in the XML, in `bindings.json`, in command-line arguments, in output, or in evidence.

## Service contracts

Gmail keeps draft ID, message ID, thread ID, and delivery confirmation distinct. The two threads hold a different number of messages, and every delivery carries its own opaque marker. The delivery check performs a single bounded search with `max_results=1` on the exact marker. No result yields `not_ready`, and nothing is retried automatically. User labels, subjects, recipients, snippets, and bodies are never read; future label tasks use system labels only, sorted by `label_id`.

Calendar uses only the primary calendar through a private reference and a fixed window from 2027-02-01 to 2027-03-01. Every event carries an exact marker and `sendUpdates=none`, and none has attendees. User calendar names and third-party availability are not part of the fixture.

Drive creates one dedicated folder and two objects inside it, each with a unique marker. Sheets creates one spreadsheet with two sheets, holding only the values and formulas its ten future tasks require. Docs creates one document and one tab with bounded synthetic text.

## Readiness check

The live check uses the same five explicit credential files as seeding:

```bash
cd app
uv run --no-sync python -m google_workspace_mcp.evals readiness \
  --bindings ../private/evals/bindings.json \
  --credentials-dir ../private/google-tokens
```

The command accepts only `applied` or `ready` bindings and rejects `planned` before reading credentials. Each of the three Gmail deliveries gets exactly one exact-marker search, and every other logical reference exactly one read of the bound object with `num_retries=0`. A missing object produces `not_ready`, exit code 1, and leaves the registry unchanged. A complete 21-item report moves the registry to `ready` through the same atomic private-file writer used by seeding. Output is limited to the fixture version, binding state, readiness status, probe count, and ready or not-ready counts; provider failures are reduced to `fixture readiness check failed` without response content, identifiers, paths, or chained exceptions.
