# Google Cloud and OAuth Setup

This document configures Google Cloud access for the five Hawkx Workspace MCP services:

- Gmail
- Calendar
- Drive
- Sheets
- Docs

Each service receives a separate Google OAuth grant and stores a separate credential file. A grant issued for one service must not be reused by another service.

## Prerequisites

- A Google account that owns the Workspace data
- A Google Cloud project
- Python 3.14
- uv
- A public HTTPS homepage
- A public Google user data privacy policy
- An authorized domain controlled by the application owner

Install the project environment before running the authorization commands:

```bash
uv sync --dev
```

## Enable Google APIs

Enable these APIs in the Google Cloud project:

| Service | API |
|---|---|
| Gmail | `gmail.googleapis.com` |
| Calendar | `calendar-json.googleapis.com` |
| Drive | `drive.googleapis.com` |
| Sheets | `sheets.googleapis.com` |
| Docs | `docs.googleapis.com` |

Verify that all five services appear in the Enabled APIs list before configuring OAuth.

## Configure Google Auth Platform

Open Google Auth Platform in the selected Google Cloud project.

### Branding

Set the application name and user support email.

Provide public URLs hosted on a domain controlled by the application owner:

```text
Application home page:
https://mcp.example.com/

Application privacy policy:
https://mcp.example.com/privacy

Authorized domain:
example.com
```

The privacy policy must describe:

- Google data accessed by the application
- How the data is used
- Storage and retention
- Sharing and disclosure
- User controls and deletion
- Security measures
- Compliance with the Google API Services User Data Policy

### Audience

Select:

```text
External
```

An External application can authorize Google accounts outside a Google Workspace organization.

### Data Access

Add exactly these OAuth scopes:

| Service | Scope | Google classification |
|---|---|---|
| Gmail | `https://www.googleapis.com/auth/gmail.modify` | Restricted |
| Calendar | `https://www.googleapis.com/auth/calendar.events` | Sensitive |
| Calendar | `https://www.googleapis.com/auth/calendar.calendarlist.readonly` | Non-sensitive |
| Calendar | `https://www.googleapis.com/auth/calendar.freebusy` | Non-sensitive |
| Drive | `https://www.googleapis.com/auth/drive.readonly` | Restricted |
| Drive, Sheets, Docs | `https://www.googleapis.com/auth/drive.file` | Non-sensitive |
| Sheets | `https://www.googleapis.com/auth/spreadsheets` | Sensitive |
| Docs | `https://www.googleapis.com/auth/documents` | Sensitive |

Do not add broader scopes. In particular:

- Do not replace `gmail.modify` with `https://mail.google.com/`
- Do not replace `drive.readonly` and `drive.file` with full Drive access
- Do not add the full Calendar scope when event access is sufficient

### OAuth Client

Create one OAuth client with this application type:

```text
Desktop app
```

Download the client JSON after creation.

The client JSON is a secret. Store it outside the project directory with owner-only permissions.

```bash
CREDENTIAL_ROOT="$HOME/.local/share/google-workspace-mcp"
CLIENT_SECRET_SOURCE="$HOME/Downloads/client_secret.json"

install -d -m 700 "$CREDENTIAL_ROOT"
install -m 600 "$CLIENT_SECRET_SOURCE" "$CREDENTIAL_ROOT/client_secret.json" && rm -f "$CLIENT_SECRET_SOURCE"
```

Do not paste the client JSON, client secret, authorization code, OAuth state, PKCE challenge, access token, or refresh token into chat, documentation, logs, or support requests.

## Publishing Status

Google OAuth publishing status and Google verification are separate states.

### Testing

Testing mode is suitable for initial setup, but Google refresh tokens may expire after approximately seven days when the application requests sensitive or restricted scopes.

### In production

Publishing the application removes the Testing-mode refresh-token lifetime restriction.

An application can be:

```text
In production
Unverified
```

In this state:

- The owner can authorize the application
- Google may display an unverified application warning
- Google may limit the number of unique users
- Google verification is still required for broad distribution
- Restricted scopes may require a security assessment

Publishing does not guarantee that refresh tokens will remain valid permanently. A refresh token can still be invalidated by user revocation, account security events, inactivity, token limits, or Google policy changes.

## OAuth User Cap

The OAuth user cap counts unique Google accounts that grant unapproved sensitive or restricted scopes.

Five service grants issued to the same Google account count as one user, not five.

The cap applies for the lifetime of the Google Cloud project. Revoking a token or deleting a local credential file does not return a user slot.

Avoid authorizing test accounts that will not use the application.

## Authorize the Five Services

Run every command from the project directory.

Set the client-secret path once:

```bash
CLIENT_SECRET_PATH="$HOME/.local/share/google-workspace-mcp/client_secret.json"
```

Authorize Gmail:

```bash
uv run --no-sync google-mcp-authorize --service gmail --client-secrets "$CLIENT_SECRET_PATH"
```

Authorize Calendar:

```bash
uv run --no-sync google-mcp-authorize --service calendar --client-secrets "$CLIENT_SECRET_PATH"
```

Authorize Drive:

```bash
uv run --no-sync google-mcp-authorize --service drive --client-secrets "$CLIENT_SECRET_PATH"
```

Authorize Sheets:

```bash
uv run --no-sync google-mcp-authorize --service sheets --client-secrets "$CLIENT_SECRET_PATH"
```

Authorize Docs:

```bash
uv run --no-sync google-mcp-authorize --service docs --client-secrets "$CLIENT_SECRET_PATH"
```

For every consent flow:

1. Select the Google account that owns the Workspace data.
2. Confirm that the application name is correct.
3. Confirm that only the expected service scopes are requested.
4. Complete the loopback callback.
5. Return to the terminal and check the final JSON line.

The authorization URL contains temporary OAuth state and PKCE parameters. Do not publish or retain that URL.

## Expected Grants

The final JSON output must report `refresh_token_present: true`.

Expected grants:

| Service | Required scopes |
|---|---|
| Gmail | `gmail.modify` |
| Calendar | `calendar.events`, `calendar.calendarlist.readonly`, `calendar.freebusy` |
| Drive | `drive.readonly`, `drive.file` |
| Sheets | `spreadsheets`, `drive.file` |
| Docs | `documents`, `drive.file` |

The command rejects a grant when:

- Google returns no refresh token
- A required scope is missing
- The client-secret file is unsafe
- The credential directory is unsafe
- The credential file cannot be stored securely

Credential directories use owner-only permissions. Credential files use mode `0600`.

## Unverified Application Warning

An In production application can still display:

```text
Google hasn't verified this app
```

Proceed only when all of the following are true:

- You created or control the application
- The displayed application name is correct
- The OAuth client belongs to the expected Google Cloud project
- The requested scopes match this document
- The callback uses a loopback address

Do not bypass an unverified application warning for an unknown application.

## Verification

Google verification is separate from publishing.

Sensitive and restricted scopes can require:

- Application identity review
- Domain ownership verification
- Scope justification
- Demonstration of the user-facing flow
- Review of the privacy policy
- Security assessment for restricted data handled by a server

The application can be used by its owner before verification, but public distribution should not rely on the unverified state.

## Revocation and Reauthorization

A user can revoke access from Google Account security settings.

After revocation:

1. Stop the affected service.
2. Remove the affected local credential file.
3. Run `google-mcp-authorize` again for that service.
4. Confirm the exact scope set.
5. Restart the service with the new credential.

Revoking one service must not require replacing the other four service grants.

## Troubleshooting

### `uv run` reports that no command was provided

The shell executed `uv run --no-sync` separately because the command was split across lines.

Run the complete authorization command on one line, or use shell continuation characters.

### `Failed to spawn: google-mcp-authorize`

Install the project entry points:

```bash
uv sync --dev
```

Then rerun the authorization command with `--no-sync`.

### No refresh token was returned

Remove the application's existing access from Google Account security settings and repeat the service authorization.

The CLI requests offline access and forces the consent screen. If Google still returns no refresh token, the command fails without storing an incomplete credential.

### The granted scopes do not match

Cancel the flow and check:

- The selected service
- The Google Cloud project
- Data Access scopes
- The OAuth client JSON
- The Google account selected in the consent screen

Do not accept extra scopes to make the flow succeed.

### The token expires after seven days

Confirm that the Google Auth Platform publishing status is:

```text
In production
```

Testing mode can issue short-lived refresh tokens for sensitive or restricted scopes.
