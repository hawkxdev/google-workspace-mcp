# Authentication and Credential Operations

Hawkx Workspace MCP uses two independent OAuth boundaries:

1. MCP clients authenticate to one MCP service.
2. That service authenticates to one Google product API.

The two token types are not interchangeable. Google credentials never authorize MCP clients, and MCP bearer tokens never authorize direct Google API calls.

## MCP Client Authorization

Each service is an OAuth authorization server and a protected MCP resource bound to its canonical HTTPS public URL.

### Discovery and OAuth routes

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/.well-known/oauth-authorization-server<resource-path>` | Authorization-server metadata |
| `GET` | `/.well-known/oauth-protected-resource<resource-path>` | Protected-resource metadata |
| `GET`, `POST` | `/oauth/authorize` | Owner login and authorization-code issuance |
| `POST` | `/oauth/token` | Authorization-code and refresh-token grants |
| `POST` | `/oauth/register` | Dynamic client registration |

Metadata advertises:

- authorization-code and refresh-token grants;
- PKCE S256;
- `client_secret_post` client authentication;
- the canonical service resource;
- the service authorization and token endpoints;
- RFC 9207 authorization-response issuer identification.

## Dynamic Client Registration

A client registers one or more redirect URIs and an optional client name.

Accepted redirect URIs are:

- HTTPS URLs;
- loopback HTTP URLs using `localhost`, `127.0.0.1`, or `::1`.

The server rejects fragments, unsupported schemes, non-loopback plain HTTP redirects, and invalid URI forms.

A successful registration returns a client ID and client secret. Store both as secrets. The server stores verifier material rather than recoverable client-secret values.

## Authorization-Code Flow

The MCP client must send:

- `response_type=code`;
- its registered client ID;
- one exact registered redirect URI;
- a PKCE challenge;
- `code_challenge_method=S256`;
- the canonical service `resource`;
- an opaque `state` value.

The owner authenticates with the service operator credentials. A successful authorization produces a one-time authorization code and redirects the browser to the registered URI. The redirect includes an `iss` parameter equal to the canonical metadata issuer so compatible clients can reject authorization-server mix-up.

The token request must include:

- the authorization code;
- client ID and client secret;
- the same redirect URI;
- the PKCE verifier;
- the same canonical resource.

Authorization codes are single-use and short-lived.

## Access Tokens

Access tokens:

- use the service token format;
- are bound to one canonical resource;
- carry one service authorization policy;
- have a bounded lifetime;
- are revoked when their client, token, or refresh family is revoked.

Send the access token only in the HTTP `Authorization` header:

```text
Authorization: Bearer <access-token>
```

Tokens in query parameters are not supported.

A token issued by one service cannot be used against another service resource.

## Refresh Token Rotation

MCP refresh tokens are single-use.

A successful refresh:

1. validates the client, resource, expiry, revocation state, and authorization policy;
2. consumes the presented refresh token;
3. creates a new access token;
4. creates a replacement refresh token in the same family.

Presenting an already consumed refresh token is treated as replay. The server revokes the complete refresh family and its associated access tokens.

A policy change can require a new interactive authorization instead of refresh.

## Bearer Enforcement

Public routes include health, OAuth discovery, registration, authorization, and token exchange.

Protected routes require one valid bearer token. The middleware rejects:

- missing credentials;
- malformed or multiple authorization headers;
- unknown token formats;
- expired or revoked tokens;
- resource mismatches;
- invalid authorization policies.

The MCP server filters the visible tool list before returning it. A readonly principal receives only tools explicitly marked readonly. A principal without permission cannot call a hidden tool by guessing its name.

Resources, resource templates, and prompts are unavailable unless the principal policy permits them.

## Readonly and Full Access

A service can issue a restricted client policy or a full operator policy.

Readonly access permits:

- authenticated MCP access;
- protected readiness;
- only service tools marked readonly.

Full access permits the complete service tool registry and any supported resources or prompts.

Authorization policy is stored with the client and tokens. It is not selected by an untrusted request parameter.

## OAuth State Storage

Each service has a separate SQLite state database containing:

- registered clients and redirect URIs;
- authorization codes;
- access-token metadata;
- refresh-token families;
- policy and capability bindings;
- resource and service ownership metadata;
- expiry and revocation state.

State storage is bound to one service and one canonical resource. Starting a service against state owned by another service or resource fails closed.

OAuth state, Google credentials, audit logs, and managed downloads must use separate paths.

## Google Service Credentials

Google credentials are stored separately from MCP OAuth state.

Each service owns one Google credential with an exact scope set. The credential store enforces:

- owner-only directories;
- files and lockfiles with mode `0600`;
- no symbolic-link traversal;
- no overlap with managed download paths;
- cross-process locking;
- atomic writes;
- exact scope validation;
- secret-safe errors.

The provider gateway refreshes Google access tokens internally. It never returns access tokens or refresh tokens to MCP clients.

See [Google Cloud and OAuth Setup](google-cloud-setup.md) for Google project configuration and initial consent.

## OAuth Administration CLI

Every administration command requires an explicit service.

Run the CLI with the same service-prefixed runtime configuration used by the service. Explicit state or download path overrides do not replace the required public URL and operator login configuration.

List clients:

```bash
uv run --no-sync google-mcp-oauth --service gmail clients list
```

Revoke a client and its active authorization state:

```bash
uv run --no-sync google-mcp-oauth --service gmail clients revoke <client-id>
```

List tokens:

```bash
uv run --no-sync google-mcp-oauth --service gmail tokens list
```

List active tokens for one client:

```bash
uv run --no-sync google-mcp-oauth --service gmail tokens list --client-id <client-id> --active-only
```

Revoke an access token by metadata ID:

```bash
uv run --no-sync google-mcp-oauth --service gmail tokens revoke <token-id>
```

Create an online OAuth state backup:

```bash
uv run --no-sync google-mcp-oauth --service gmail backup /secure/backups/gmail-oauth.sqlite3
```

List operations return metadata only. They do not return token values or client secrets.

## Revocation Procedures

### Revoke one MCP client

1. List the service clients.
2. Identify the client metadata record.
3. Revoke that client.
4. Confirm that its access and refresh state is no longer active.
5. Reauthorize the client when access is required again.

Revoking a client from one service does not revoke the same application from the other four services.

### Revoke one Google service grant

1. Stop the affected service.
2. Revoke the application in Google Account security settings.
3. Remove the affected service credential file.
4. Run `google-mcp-authorize` again for that service.
5. Confirm the exact returned scope set.
6. Restart the service.

The other four service grants remain unchanged.

## OAuth Error Contract

| Condition | HTTP result | OAuth error |
|---|---:|---|
| Malformed authorization request | `400` | `invalid_request` |
| Unknown or rejected client | `401` | `invalid_client` |
| Wrong canonical resource | `400` or `401` | `invalid_target` |
| Invalid, expired, reused, or revoked grant | `400` | `invalid_grant` |
| Missing bearer token | `401` | Bearer challenge |
| Invalid bearer token | `401` | `invalid_token` |
| Valid token without required capability | `403` | `insufficient_scope` |

A `401` bearer response includes protected-resource metadata so compatible MCP clients can discover the correct authorization server.

## Security Requirements

- Use HTTPS for every public service resource.
- Keep owner login credentials outside the project directory.
- Keep client IDs and client secrets out of documentation and logs.
- Never place OAuth state, Google credentials, audit logs, or backups inside managed downloads.
- Do not reuse state databases or Google credential files between services.
- Do not copy bearer tokens into query strings.
- Treat refresh-token replay as credential compromise.
- Back up OAuth state before destructive administration.
- Keep backup destinations owner-controlled and outside public files.
