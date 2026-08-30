# MCP Protocol Contract

Each Hawkx Workspace MCP service exposes one stateless Streamable HTTP MCP endpoint (`/<service>/mcp`) and an OAuth 2.1 authorization surface under its service-base issuer (`https://<host>/<service>`).
The five services use the same transport contract but different resources, paths, tools, credentials, and OAuth state.

## Transport Model

The MCP transport uses:

- Streamable HTTP;
- one configured MCP path per service;
- stateless HTTP operation;
- JSON responses;
- MCP protocol version `2025-06-18`;
- bearer authorization on protected requests;
- DNS rebinding protection;
- exact host and origin allowlists.

The default MCP paths are:

| Service | MCP path |
|---|---|
| Gmail | `/gmail/mcp` |
| Calendar | `/calendar/mcp` |
| Drive | `/drive/mcp` |
| Sheets | `/sheets/mcp` |
| Docs | `/docs/mcp` |

The MCP path can be configured per service. It cannot collide with OAuth, health, readiness, or extension routes.

## HTTP Surface

| Method | Route | Authentication | Purpose |
|---|---|---|---|
| `GET` | `/health` (proxied as `/<service>/health`) | Public | Process liveness |
| `GET` | `/ready` (proxied as `/<service>/ready`) | Bearer token | Service readiness |
| `GET`, `POST` | `/<service>/mcp` | Bearer token | Streamable HTTP MCP (canonical protected resource) |
| `GET` | `/.well-known/oauth-authorization-server/<service>` | Public | OAuth authorization-server discovery (service-base) |
| `GET` | `/.well-known/oauth-protected-resource/<service>/mcp` | Public | Protected-resource metadata discovery (MCP resource) |
| `GET`, `POST` | `/<service>/oauth/authorize` | OAuth flow | Owner authorization |
| `POST` | `/<service>/oauth/token` | OAuth flow | Code and refresh grants |
| `POST` | `/<service>/oauth/register` | Public registration | Dynamic client registration |
The production reverse proxy exposes process liveness and readiness as `/<service>/health` and `/<service>/ready`, rewriting them to the process-local `/health` and `/ready` routes. Readiness remains bearer protected.

When the MCP path is not `/`, a root `GET` or `HEAD` probe returns `200` and identifies the supported MCP protocol version.

## Health and Readiness

### Health

`GET /health` is public and returns process identity and liveness.

```json
{"service":"gmail","status":"ok"}
```

A successful health response does not prove that Google credentials are usable.

### Readiness

`GET /ready` is protected. A valid bearer token with an accepted policy is required.

```json
{"status":"ready"}
```

Readiness confirms that the authenticated service surface is available. It does not execute a Google operation.

## OAuth Discovery

The authorization-server metadata route is scoped to the service-base issuer path:

```text
/.well-known/oauth-authorization-server/<service>
```

The protected-resource metadata route is scoped to the canonical protected resource path:

```text
/.well-known/oauth-protected-resource/<service>/mcp
```

Authorization-server metadata advertises:

- canonical service-base issuer (`https://<host>/<service>`);
- RFC 9207 authorization-response issuer support;
- authorization endpoint (`https://<host>/<service>/oauth/authorize`);
- token endpoint (`https://<host>/<service>/oauth/token`);
- registration endpoint (`https://<host>/<service>/oauth/register`);
- authorization-code and refresh-token grants;
- PKCE S256;
- `client_secret_post`.

Successful authorization redirects include an `iss` parameter that exactly matches the canonical service-base metadata issuer.

Protected-resource metadata advertises:

- the canonical protected resource (`https://<host>/<service>/mcp`);
- its authorization server (`https://<host>/<service>`);
- bearer tokens in the HTTP header.

See [Authentication and Credential Operations](auth.md) for authorization and token lifecycle details.

## Bearer Requests

Protected requests use one header:

```text
Authorization: Bearer <access-token>
```

The server rejects:

- query-string tokens;
- malformed authorization headers;
- multiple authorization headers;
- unknown token formats;
- expired or revoked tokens;
- tokens bound to another resource;
- tokens with an invalid authorization policy.

Each accepted request receives request-scoped principal metadata. The context is removed after the request completes.

## Authorization Challenges

An unauthenticated protected request returns `401` and a bearer challenge containing the protected-resource metadata URL (`/.well-known/oauth-protected-resource/<service>/mcp`).

A malformed bearer request returns `400` with `invalid_request`.

An invalid, expired, revoked, or resource-mismatched token returns `401` with `invalid_token`.

A valid token without the required capability returns `403` with `insufficient_scope`.

The challenge can include the readonly scope required by the service policy.

## Tool Discovery and Calls

The server filters tools before returning `tools/list`.

A principal sees only tools allowed by its authorization policy. A hidden tool cannot be called by guessing its name.

Readonly clients receive only tools explicitly marked readonly. Full-access clients receive the complete service tool registry.

The same policy applies before tool execution. Unauthorized calls fail without invoking the provider gateway.

Resources, resource templates, and prompts are hidden unless the principal policy permits them.

## Request Validation

Tool handlers validate public domain input before calling Google.

Validation includes:

- required identifiers;
- bounded page sizes;
- bounded text and payload sizes;
- typed enum values;
- timestamp and range formats;
- service-specific concurrency fields;
- managed-file names and paths.

Raw Google request dictionaries, raw field masks, arbitrary URLs, and unbounded provider options are not accepted as public tool inputs.

## Provider Boundary

A tool calls one service provider gateway.

The gateway:

1. refreshes the service-specific Google credential;
2. creates the Google API client;
3. executes the provider request;
4. validates the provider response shape;
5. converts the response into typed service schemas;
6. maps provider errors to safe service errors.

Raw provider responses and credential values do not cross the MCP boundary.

## Pagination

Gmail, Calendar, and Drive list operations use client-driven pagination.

A request provides:

- a bounded page size;
- an optional provider page token.

A response returns:

- a bounded item list;
- `next_page_token` when another page exists.

Clients decide whether to request the next page. The service does not follow provider pagination without a client request.

## Docs Continuation

Docs content uses UTF-16 continuation rather than provider page tokens.

A bounded read response includes:

- the document revision;
- the explicit tab identifier;
- returned UTF-16 start and end indices;
- typed content blocks;
- the number of returned text characters;
- `truncated`;
- `next_start_index` when truncated;
- unsupported structure kinds.

Continue by sending `next_start_index` as the next UTF-16 start index.

Continuation can resume inside one large paragraph or table without repeating the complete block. A surrogate pair is never split.

## Response Bounds

Responses are bounded at the service layer before they reach the MCP client.

Examples include:

- Gmail result and message-body limits;
- Calendar page and time-window limits;
- Drive page and download limits;
- Sheets range, cell, grid, and payload limits;
- Docs block, character, node, tab, and nesting limits.

The service returns an explicit input or unsupported-profile error when a request or provider result exceeds its supported contract.

## Write Semantics

Mutation tools validate input and provider preconditions before sending a write.

A stale version, revision, or precondition produces a conflict rather than silently overwriting newer state.

Writes are not blindly retried when a transport or provider failure leaves the outcome uncertain.

For an indeterminate write:

1. do not repeat the mutation immediately;
2. reread the target resource;
3. compare its current version or revision;
4. decide whether the intended change already landed;
5. submit a new mutation only when safe.

This behavior is explicit for Docs writes and applies wherever a service reports a possibly-applied outcome.

## Docs Mutation Contract

Docs mutations use:

- one explicit tab;
- half-open UTF-16 ranges;
- the latest known document revision;
- typed operations;
- bounded batches.

A batch runs operations in caller order, but every index is interpreted against the supplied revision.

A batch contains at most one literal replacement. A replacement cannot be combined with operations that shift indices.

The mandatory final newline cannot be deleted, and a UTF-16 index cannot split a surrogate pair.

## Managed File Results

Gmail attachments, Drive downloads, and Drive exports return metadata for a file written inside the service managed-file directory.

A managed-file result can include:

- service-generated file name;
- byte size;
- MIME type;
- SHA-256 digest.

The response does not expose arbitrary local filesystem paths outside the managed boundary.

## Error Contract

Errors are mapped to MCP-safe messages and service-specific types.

| Class | Meaning |
|---|---|
| Input error | Request violates the public schema or a service bound |
| Authentication error | Bearer or Google credential is missing, invalid, or revoked |
| Scope error | Credential lacks the exact service scopes |
| Not found | Requested Google resource does not exist or is inaccessible |
| Conflict | Version, revision, or provider precondition is stale |
| Rate limit | Google rejected the request due to quota or rate limits |
| Provider error | Google returned an unexpected safe-to-report failure |
| Unsupported | Resource structure is outside the supported profile |
| Indeterminate write | A write may already have been applied |

Provider URLs, raw error bodies, tokens, client secrets, authorization codes, and private Google content are not included in public errors.

## Service Isolation

Each process owns:
- one service-base issuer (`https://<host>/<service>`);
- one canonical protected resource URL (`https://<host>/<service>/mcp`);
- one MCP path (`/<service>/mcp`);
- one OAuth state database;
- one Google credential path;
- one scope set;
- one audit target;
- one managed-file directory;
- one tool registry.

Configuration fails when protected paths overlap or stored OAuth ownership does not match the configured service and resource.

A token or Google credential from another service is rejected before the requested cross-service data is returned.

## Protocol Version

The supported MCP protocol version is:

```text
2025-06-18
```

Clients should send the MCP protocol-version header required by the Streamable HTTP specification and use the service discovery metadata instead of hardcoding authorization endpoints.
