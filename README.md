# google-workspace-mcp

A project for building five independent remote MCP servers for Google Workspace: Gmail, Google Calendar, Google Drive, Google Sheets, and Google Docs.

> **Status: pre-alpha.** The repository contains five locally runnable MCP service processes with empty tool registries, isolated OAuth state and audit targets, OAuth-only bearer middleware, path-scoped OAuth routes, secure health and readiness endpoints, a metadata-only OAuth administration CLI, and immutable per-service configuration. Google API integration and service tools are not implemented yet.

## Current status

Implemented:

- a Python package with five service entry points and one OAuth administration entry point;
- SQLite-backed downstream OAuth state;
- OAuth client registration and PKCE;
- token binding to a canonical `resource`;
- refresh token rotation, replay detection, and family revocation;
- immutable state ownership metadata for the service and resource;
- immutable per-service configuration with strict port and token TTL validation;
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

All five service entry points run local MCP applications with empty tool registries. The OAuth administration entry point is operational. Google API integration, service tools, deployment configuration, and production credentials are not implemented yet.

## Planned services

The table describes the target scope. These capabilities are not available yet.

| Service | Target capabilities |
|---|---|
| `gmail` | search and read messages and threads, labels, attachments, drafts, send, and reply |
| `calendar` | calendars, event search and retrieval, availability, event creation and updates |
| `drive` | file search, metadata, folder contents, download, and export |
| `sheets` | spreadsheet metadata, range reads and writes, batch operations |
| `docs` | document creation, structure and text retrieval, insert, and replace |

In the target architecture, each service runs as a separate process with its own MCP endpoint, tool registry, Google OAuth scopes, Google credentials, and downstream OAuth state.

## Target authorization architecture

The design uses two independent layers:

**Client to service.** The MCP client uses OAuth 2.1 with PKCE. Each access token is bound to one `resource`, and refresh tokens rotate with replay detection.

**Service to Google.** Each service uses separate Google credentials and the minimum required OAuth scopes. Google refresh tokens are never returned to MCP clients.

The downstream state core, OAuth-only bearer middleware, OAuth endpoint routes, transport composition, and five isolated service factories exist today. The Google authorization layer and service tools are not implemented.

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

A Google Cloud project and OAuth client will be required after the Google API integration is implemented. The current version does not use them.

## Development setup

```bash
git clone https://github.com/hawkxdev/google-workspace-mcp.git
cd google-workspace-mcp
uv sync --dev
```

The virtual environment does not need to be activated. Run commands through `uv run`.

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
| `src/google_workspace_mcp/audit/` | fail-closed per-service audit logging |
| `src/google_workspace_mcp/transport/` | MCP policy, Streamable HTTP composition, and shared factory |
| `src/google_workspace_mcp/services/` | five thin isolated service factories |
| `src/google_workspace_mcp/cli/` | five runnable service entry points, shared runner, and OAuth administration |
| `tests/core/` | OAuth core and package entry point regressions |
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
