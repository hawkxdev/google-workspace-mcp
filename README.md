# google-workspace-mcp

A project for building five independent remote MCP servers for Google Workspace: Gmail, Google Calendar, Google Drive, Google Sheets, and Google Docs.

> **Status: pre-alpha.** The repository contains five locally runnable MCP service processes with isolated OAuth state, audit targets, path-scoped OAuth routes, secure health and readiness endpoints, immutable per-service configuration, a tested Google credential layer, 18 Gmail tools, 9 Calendar tools, 10 Drive tools, and 11 Sheets tools. Docs tool registry remains empty. Production Google credentials and deployment configuration are not included.

## Current status

Implemented:

- a Python package with five service entry points and one OAuth administration entry point;
- SQLite-backed downstream OAuth state;
- OAuth client registration and PKCE;
- token binding to a canonical `resource`;
- refresh token rotation, replay detection, and family revocation;
- immutable state ownership metadata for the service and resource;
- immutable per-service configuration with strict port and token TTL validation;
- secure per-service Google credential storage with atomic writes, cross-process refresh locking, scope validation, bounded retries, and secret-safe errors;
- OAuth-only bearer authentication with RFC 9728 challenges;
- path-scoped OAuth metadata, authorization, token, and registration routes;
- request-scoped, secret-free authenticated principal metadata;
- fail-closed binding between service configuration and OAuth state ownership;
- a metadata-only OAuth administration CLI with service ownership checks;
- MCP 2.x Streamable HTTP application composition;
- five isolated service factories and runnable CLI entry points;
- public minimal health and protected readiness endpoints;
- fail-closed per-service audit logging and startup validation;
- exact trusted-proxy allowlists without wildcard forwarding trust.
- 18 Gmail tools for bounded search and reading, label workflows, managed attachment downloads, full draft lifecycle, plain-text sending, and reply to the original author.
- 9 Calendar tools for calendar lists, bounded event search and reads, free/busy, event CRUD, recurring-event scopes, and mixed batch mutations.
- 10 Drive tools for structured file and folder search, metadata retrieval, managed binary downloads, format-validated exports, folder creation, managed uploads, version-preflighted updates, moves, and app-owned copies.
- 11 Sheets tools for spreadsheet metadata, single and batch A1 range reads with explicit render and date/time formatting modes, single and batch range updates with explicit raw/user-entered parsing, table row appending with insert/overwrite controls, destructive range clearing, and sheet structure management (create, add, rename, copy).

The Gmail, Calendar, Drive, and Sheets entry points register service-owned tools and connect them to separate Google credential boundaries. Docs continues to run with an empty tool registry. Production Google credentials and deployment configuration are not included.

## Service status

| Service | Status | Capabilities |
|---|---|---|
| `gmail` | Implemented locally | bounded message and thread search and reads, labels, managed attachment downloads, drafts, plain-text send, and reply |
| `calendar` | Implemented locally | calendar list, bounded event search and reads, free/busy, event CRUD, recurring-event scopes, and batch mutations |
| `drive` | Implemented locally | structured search, metadata, folder contents, managed binary downloads, exports, folder creation, managed uploads, versioned updates, moves, and app-owned copies |
| `sheets` | Implemented locally | spreadsheet metadata, single/batch range reads, single/batch range writes, table row appending, range clearing, and sheet structure management |
| `docs` | Planned | document creation, structure and text retrieval, insert, and replace |

Each service runs as a separate process with its own MCP endpoint, tool registry, Google OAuth scopes, Google credentials, and downstream OAuth state.

## Target authorization architecture

The design uses two independent layers:

**Client to service.** The MCP client uses OAuth 2.1 with PKCE. Each access token is bound to one `resource`, and refresh tokens rotate with replay detection.

**Service to Google.** Each service uses separate Google credentials and the minimum required OAuth scopes. Google refresh tokens are never returned to MCP clients.

The downstream state core, OAuth-only bearer middleware, OAuth endpoint routes, transport composition, five isolated service factories, Google credential layer, Gmail tools, Calendar tools, Drive tools, and Sheets tools exist today. The Docs service tool set remains planned.

## Technology

- [Python 3.14](https://docs.python.org/3.14/)
- [uv](https://docs.astral.sh/uv/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Starlette](https://www.starlette.io/)
- [Pydantic](https://docs.pydantic.dev/)
- [Google API Python Client](https://github.com/googleapis/google-api-python-client)

## Development prerequisites

- Git
- Python 3.14
- uv

A Google Cloud project and OAuth client will be required when production credentials are provisioned. The current credential-layer tests use local fake token endpoints and synthetic credentials only.

## Development setup

```bash
git clone https://github.com/hawkxdev/google-workspace-mcp.git
cd google-workspace-mcp
uv sync --dev
```

The virtual environment does not need to be activated. Run commands through `uv run`.

## Configuration

Every service reads its settings from environment variables prefixed with the service name, for example `GMAIL_` or `DRIVE_`.

`<SERVICE>_MCP_PUBLIC_URL` is required and must be the absolute HTTPS URL the service is reachable at. There is no default: OAuth metadata, token audience binding, and the advertised endpoints are all derived from it.

`<SERVICE>_MCP_ALLOWED_HOSTS` lists the public host names the transport accepts. Leave it unset for local development, where the transport stays reachable on loopback only. Behind a reverse proxy it must contain the public host, otherwise the proxied request is rejected before it reaches the application.

`<SERVICE>_MCP_FORWARDED_ALLOW_IPS` is an exact list of trusted proxy addresses. Wildcards and unbounded networks such as `0.0.0.0/0` are rejected.

`<SERVICE>_OAUTH_STATE_PATH`, `<SERVICE>_GOOGLE_TOKEN_PATH`, `<SERVICE>_AUDIT_LOG_PATH`, and `<SERVICE>_MCP_DOWNLOAD_PATH` must all differ from one another.

## OAuth administration

The metadata-only operator CLI lists and revokes clients or access tokens and creates online SQLite backups. Every command requires an explicit service and validates the persisted state owner before operating. Client secrets, authorization codes, and token values are never returned.

```bash
uv run --no-sync google-mcp-oauth --service gmail clients list
```

Each service uses its own `<SERVICE>_OAUTH_STATE_PATH` and `<SERVICE>_MCP_DOWNLOAD_PATH`. The download path is a security boundary: OAuth state and backups are rejected inside it.

## Checks

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src
```

The `--no-sync` flag is required when checking the installed dependency version. A plain `uv run` may resynchronize the environment from the lock file.

## Structure

| Path | Purpose |
|---|---|
| `src/google_workspace_mcp/auth/state.py` | downstream OAuth state lifecycle |
| `src/google_workspace_mcp/auth/bearer.py` | OAuth-only bearer middleware and RFC 9728 challenges |
| `src/google_workspace_mcp/auth/context.py` | request-scoped authenticated principal metadata |
| `src/google_workspace_mcp/auth/oauth.py` | OAuth metadata, authorization, token, and registration routes |
| `src/google_workspace_mcp/common/config.py` | immutable per-service environment configuration |
| `src/google_workspace_mcp/common/retry.py` | bounded retry policy for Google credential refresh |
| `src/google_workspace_mcp/google_auth/` | secure Google credential persistence, refresh, and scope validation |
| `src/google_workspace_mcp/audit/` | fail-closed per-service audit logging |
| `src/google_workspace_mcp/transport/` | MCP policy, Streamable HTTP composition, and shared factory |
| `src/google_workspace_mcp/services/` | five isolated service factories plus Gmail, Calendar, Drive, and Sheets domain tools |
| `src/google_workspace_mcp/cli/` | five runnable service entry points, shared runner, and OAuth administration |
| `tests/core/` | OAuth core and package entry point regressions |
| `tests/services/` | Gmail, Calendar, Drive, and Sheets provider, domain, authorization, tool, and factory regressions |
| `pyproject.toml` | package metadata, dependencies, and tool configuration |
| `NOTICE` | provenance of adapted code |

## Contributing

The project is in early development. Before making a substantial change, open an [issue](https://github.com/hawkxdev/google-workspace-mcp/issues) describing the proposed behavior. Changes must not merge the five services into one process, share credentials between services, or introduce an OAuth bypass.

## Provenance

The project adapts the OAuth 2.1 core from `jimprosser/obsidian-web-mcp` under the MIT License. See `NOTICE` for details.

The downstream OAuth 2.1 core is based on revision `7e6a52d791a50e3bd533df1060217973ab5be1c8`. It includes additional fixes for client and token lifecycles, refresh token rotation, replay detection, and `resource` canonicalization.

The original project's Obsidian storage and Git synchronization functionality was not copied.

This project uses `mcp>=2,<3` and composes remote applications with `mcp.server.mcpserver.MCPServer`; the removed `mcp.server.fastmcp` package is not required.

## Author

[Sergey Sokolkin (@hawkxdev)](https://github.com/hawkxdev)

## License

MIT, see `LICENSE`.
