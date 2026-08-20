# google-workspace-mcp

A project for building five independent remote MCP servers for Google Workspace: Gmail, Google Calendar, Google Drive, Google Sheets, and Google Docs.

> **Status: pre-alpha.** The MCP servers are not operational yet. The repository currently contains the package scaffold, five placeholder entry points, and the downstream OAuth state core.

## Current status

Implemented:

- a Python package with five console entry points;
- SQLite-backed downstream OAuth state;
- OAuth client registration and PKCE;
- token binding to a canonical `resource`;
- refresh token rotation, replay detection, and family revocation;
- immutable state ownership metadata for the service and resource.

All five console entry points are placeholders and exit with a message that the service is not built. The HTTP/MCP transport, Google API integration, service tools, and deployment configuration are not implemented yet.

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

Only the state core for the first layer exists today. HTTP endpoints and the second authorization layer are not implemented.

## Technology

- [Python 3.14](https://docs.python.org/3.14/)
- [uv](https://docs.astral.sh/uv/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
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
| `src/google_workspace_mcp/cli/` | five placeholder entry points |
| `tests/core/` | OAuth core and package entry point regressions |
| `pyproject.toml` | package metadata, dependencies, and tool configuration |
| `NOTICE` | provenance of adapted code |

## Contributing

The project is in early development. Before making a substantial change, open an [issue](https://github.com/hawkxdev/google-workspace-mcp/issues) describing the proposed behavior. Changes must not merge the five services into one process, share credentials between services, or introduce an OAuth bypass.

## Provenance

The project adapts the OAuth 2.1 core from `jimprosser/obsidian-web-mcp` under the MIT License. See `NOTICE` for details.

The downstream OAuth 2.1 core is based on revision `7e6a52d791a50e3bd533df1060217973ab5be1c8`. It includes additional fixes for client and token lifecycles, refresh token rotation, replay detection, and `resource` canonicalization.

The original project's Obsidian storage and Git synchronization functionality was not copied.

This project uses `mcp>=2,<3`. The `mcp.server.fastmcp` package removed in MCP 2.0 is not required; future composition will use `mcp.server.mcpserver`.

## Author

[Sergey Sokolkin (@hawkxdev)](https://github.com/hawkxdev)

## License

MIT, see `LICENSE`.
