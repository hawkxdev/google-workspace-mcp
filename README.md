# google-workspace-mcp

Five isolated remote MCP services for Gmail, Google Calendar, Google Drive, Google Sheets, and Google Docs. Each service runs as a separate process with its own endpoint, OAuth state, Google credential, scope set, and tool registry.

> **Status: pre-alpha.** All five service processes and 55 service-owned tools are locally runnable. Production revision `7bac940` is deployed as five isolated loopback services behind the public HTTPS vhost; production Google credentials remain server-only and are never included in the repository.

## Public pages

- [Homepage](https://mcp.hawkxdev.dev/)
- [Google user data privacy policy](https://mcp.hawkxdev.dev/privacy)

The public HTTPS surface serves the homepage and privacy policy together with five path-scoped MCP and OAuth runtime surfaces.

## Included

- Five independent Streamable HTTP MCP service entry points
- OAuth 2.1 client authorization with PKCE S256
- Resource-bound bearer tokens and rotating refresh tokens
- Replay detection and refresh-family revocation
- Five isolated Google credential stores
- Exact per-service Google OAuth scope validation
- Public health and protected readiness endpoints
- Fail-closed service and resource ownership checks
- Exact trusted-proxy allowlists
- Secret-safe provider error boundaries
- Managed Gmail attachment and Drive download storage
- OAuth state administration CLI
- Google installed-application authorization CLI
- Cross-service cutover safety and state transition CLI
- A hardened systemd template, five isolated environment examples, and active, maintenance, candidate, and bootstrap nginx assets

## Service capabilities

| Service | Tools | Capabilities |
|---|---:|---|
| Gmail | 18 | bounded message and thread search and reads, labels, managed attachment downloads, drafts, plain-text send, and reply |
| Calendar | 9 | calendar list, bounded event search and reads, free/busy, event CRUD, recurring-event scopes, and batch mutations |
| Drive | 10 | structured search, metadata, folder contents, managed downloads, exports, folder creation, uploads, versioned updates, moves, and app-owned copies |
| Sheets | 11 | spreadsheet metadata, single and batch range reads and writes, row appending, range clearing, and sheet structure management |
| Docs | 7 | recursive tab metadata, bounded typed reads, document creation, text insertion, replacement, range deletion, and typed atomic batches |

Detailed tool names, limits, concurrency, continuation, and error behavior are documented in [Google Workspace integrations](docs/integrations.md).

## Documentation

- [Architecture overview](docs/overview.md)
- [Google Cloud and OAuth setup](docs/google-cloud-setup.md)
- [Authentication and credential operations](docs/auth.md)
- [Google Workspace integrations](docs/integrations.md)
- [MCP protocol contract](docs/protocol.md)
- [Production deployment](deploy/README.md)

## Architecture

The project separates two authorization layers:

1. An MCP client authenticates to one service through OAuth 2.1.
2. That service authenticates to one Google product API through its own Google credential.

Google credentials never cross the MCP boundary. An MCP bearer token is accepted only by the canonical protected MCP resource that issued it.

Each process owns its MCP endpoint, Google scopes, Google credential, downstream OAuth state, audit target, managed-file directory, and tool registry.

See [Architecture overview](docs/overview.md) for the complete data flow and source layout.

## Technology

- [Python 3.14](https://docs.python.org/3.14/)
- [uv](https://docs.astral.sh/uv/)
- [MCP Python SDK](https://modelcontextprotocol.io/docs/sdk)
- [Starlette](https://www.starlette.io/)
- [Pydantic](https://docs.pydantic.dev/)
- [Google API Python Client](https://googleapis.github.io/google-api-python-client/)

## Prerequisites

- Python 3.14
- uv
- A Google Cloud project
- A Desktop OAuth client
- A public HTTPS service URL

See [Google Cloud and OAuth setup](docs/google-cloud-setup.md) before creating service credentials.

## Development setup

Run these commands from the source directory:

```bash
cd google-workspace-mcp
uv sync --dev
```

The virtual environment does not need to be activated. Run project commands through `uv run`.

## Runtime configuration

Every service reads variables with its uppercase prefix:

```text
GMAIL_
CALENDAR_
DRIVE_
SHEETS_
DOCS_
```

`<SERVICE>` below means one of those five prefixes.

| Variable | Required | Default |
|---|---|---|
| `<SERVICE>_MCP_PUBLIC_URL` | yes | none |
| `<SERVICE>_OAUTH_LOGIN_USERNAME` | yes | none |
| `<SERVICE>_OAUTH_LOGIN_PASSWORD` | yes | none |
| `<SERVICE>_MCP_HOST` | no | `127.0.0.1` |
| `<SERVICE>_MCP_PORT` | no | service default |
| `<SERVICE>_MCP_PATH` | no | `/<service>/mcp` |
| `<SERVICE>_MCP_ALLOWED_HOSTS` | no | empty for local loopback |
| `<SERVICE>_MCP_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` |
| `<SERVICE>_OAUTH_STATE_PATH` | no | `~/.local/share/google-workspace-mcp/<service>/oauth_state.sqlite3` |
| `<SERVICE>_GOOGLE_TOKEN_PATH` | no | `~/.local/share/google-workspace-mcp/<service>/google_token.json` |
| `<SERVICE>_AUDIT_LOG_PATH` | no | `~/.local/share/google-workspace-mcp/<service>/audit.jsonl` |
| `<SERVICE>_MCP_DOWNLOAD_PATH` | no | `~/.local/share/google-workspace-mcp/<service>/downloads` |
| `<SERVICE>_OAUTH_ACCESS_TOKEN_TTL_SECONDS` | no | `86400` |
| `<SERVICE>_OAUTH_REFRESH_TOKEN_TTL_SECONDS` | no | `2592000` |

The public URL must be an absolute HTTPS URL identifying the service-base issuer (for example `https://mcp.hawkxdev.dev/gmail`). The canonical protected MCP resource (`/<service>/mcp`), OAuth metadata, bearer-token resource binding, and advertised endpoints are derived from it.

OAuth state, Google credentials, audit logs, and managed downloads must use distinct paths.

`MCP_FORWARDED_ALLOW_IPS` accepts only explicit trusted proxy addresses or bounded networks. Wildcards and unbounded networks are rejected.

### Default service endpoints

| Service | Command | Port | MCP path |
|---|---|---:|---|
| Gmail | `google-mcp-gmail` | `8431` | `/gmail/mcp` |
| Calendar | `google-mcp-calendar` | `8432` | `/calendar/mcp` |
| Drive | `google-mcp-drive` | `8433` | `/drive/mcp` |
| Sheets | `google-mcp-sheets` | `8434` | `/sheets/mcp` |
| Docs | `google-mcp-docs` | `8435` | `/docs/mcp` |

Each entry point requires its complete service-prefixed configuration before startup.

## Google authorization

Each service requires a separate Google OAuth grant.

```bash
uv run --no-sync google-mcp-authorize --service gmail --client-secrets "$HOME/.local/share/google-workspace-mcp/client_secret.json"
```

The command requests offline access and rejects grants without a refresh token or the complete service scope set.

See [Google Cloud and OAuth setup](docs/google-cloud-setup.md) for API enablement, scopes, consent, publishing, verification, credential permissions, and troubleshooting.

## MCP client authorization

Each service exposes OAuth 2.1 discovery, dynamic client registration, authorization-code exchange, rotating refresh tokens, and resource-bound bearer access.

See [Authentication and credential operations](docs/auth.md) for client authorization, token lifecycle, revocation, and backup commands.

## OAuth administration

List one service's registered clients:

```bash
uv run --no-sync google-mcp-oauth --service gmail clients list
```

The CLI can list and revoke clients or access tokens and create online OAuth state backups. It returns metadata only, never token values or client secrets.

## Source layout

```text
src/google_workspace_mcp/
├── auth/
├── audit/
├── cli/
├── common/
├── google_auth/
├── services/
└── transport/

deploy/
├── README.md
├── check-cutover-ingress.sh
├── env/
├── google-mcp@.service
├── nginx-google-workspace-mcp-active.inc
├── nginx-google-workspace-mcp-bootstrap.conf
├── nginx-google-workspace-mcp-candidate.conf
├── nginx-google-workspace-mcp-maintenance.inc
├── nginx-google-workspace-mcp.conf
└── public/
docs/
├── overview.md
├── google-cloud-setup.md
├── auth.md
├── integrations.md
└── protocol.md
```

## Current boundaries

- Production revision `7bac940` is deployed through one systemd template and five isolated instances.
- Production Google credentials are stored only in per-service owner-only files on the managed host.
- The homepage, privacy policy, MCP routes, OAuth routes, metadata, health, and readiness share one HTTPS vhost without sharing process state.
- Google OAuth publishing and verification are separate states.
- Restricted Google scopes may require verification and a security assessment.
- Refresh tokens can be revoked or invalidated by Google.
- The project calls stable Gmail, Calendar, Drive, Sheets, and Docs APIs directly.
- Google Developer Preview MCP endpoints are not runtime dependencies.
- Irreversible Gmail deletion is not supported.
- Full Calendar administration and permission management are not supported.
- Raw Google request mappings and arbitrary provider field masks are not public tool inputs.

## Author

[Sergey Sokolkin (@hawkxdev)](https://github.com/hawkxdev)

## License and provenance

The project is licensed under MIT. Its OAuth 2.1 core adapts the public `obsidian-web-mcp` project; copyright and attribution details are recorded in [NOTICE](NOTICE).
