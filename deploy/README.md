# Production Deployment

This directory contains production assets for five isolated Google Workspace MCP services. The services share one package and one HTTPS virtual host, but each process owns its port, service-base issuer, canonical protected resource, OAuth state, Google credential, audit log, download directory, and environment file.

Production revision `7bac940` is deployed as five isolated loopback services behind the public HTTPS vhost. The cutover procedure and safety assets describe the transition mechanism; no cutover has occurred yet.

Running these commands changes a production host. Review the rendered files and the rollback procedure before execution. Repository checkout, package publication, credential creation, DNS changes, certificate issuance, firewall changes, and deployment are not automated by these assets.

## Runtime layout

| Path | Owner and mode | Purpose |
|---|---|---|
| `/opt/google-workspace-mcp/app` | `root:root`, directories `0755`, files `0644` | Committed source and `.venv` |
| `/opt/google-workspace-mcp/python` | `root:root`, directories `0755`, binaries executable | uv managed Python 3.14 |
| `/opt/google-workspace-mcp/public` | existing `root:root` tree | Homepage, privacy policy, and static assets |
| `/etc/google-mcp` | `root:root`, `0700` | Five systemd environment files |
| `/etc/google-mcp/<service>.env` | `root:root`, `0600` | One service configuration |
| `/var/lib/google-workspace-mcp` | `googlemcp:googlemcp`, `0700` | Service home and state root |
| `/var/lib/google-workspace-mcp/<service>` | `googlemcp:googlemcp`, `0700` | OAuth state, Google credential, and downloads |
| `/var/lib/google-workspace-mcp/<service>/google_token.json` | `googlemcp:googlemcp`, `0600` | Existing service Google credential |
| `/var/log/google-workspace-mcp/<service>` | `googlemcp:googlemcp`, `0700` | Service audit directory |
| `/etc/systemd/system/google-mcp@.service` | `root:root`, `0644` | Five systemd instances |

The service user cannot modify the root owned source, virtual environment, managed Python, or public tree. `ProtectSystem=full` protects system paths but does not make `/opt` read only. The ownership and modes above provide that boundary.

## Services

| Service | Unit | Loopback | Service-base issuer | MCP path | Canonical protected resource |
|---|---|---:|---|---|---|
| Gmail | `google-mcp@gmail` | `127.0.0.1:8431` | `https://mcp.hawkxdev.dev/gmail` | `/gmail/mcp` | `https://mcp.hawkxdev.dev/gmail/mcp` |
| Calendar | `google-mcp@calendar` | `127.0.0.1:8432` | `https://mcp.hawkxdev.dev/calendar` | `/calendar/mcp` | `https://mcp.hawkxdev.dev/calendar/mcp` |
| Drive | `google-mcp@drive` | `127.0.0.1:8433` | `https://mcp.hawkxdev.dev/drive` | `/drive/mcp` | `https://mcp.hawkxdev.dev/drive/mcp` |
| Sheets | `google-mcp@sheets` | `127.0.0.1:8434` | `https://mcp.hawkxdev.dev/sheets` | `/sheets/mcp` | `https://mcp.hawkxdev.dev/sheets/mcp` |
| Docs | `google-mcp@docs` | `127.0.0.1:8435` | `https://mcp.hawkxdev.dev/docs` | `/docs/mcp` | `https://mcp.hawkxdev.dev/docs/mcp` |
## Preconditions

Verify on the target host before changing files:

```bash
set -euo pipefail
id googlemcp || true
ss -tlnp
nginx -v
nginx -t
systemctl is-active nginx vault-mcp alert-relay
curl -fsS https://mcp.hawkxdev.dev/ >/dev/null
curl -fsS https://mcp.hawkxdev.dev/privacy >/dev/null
curl -fsS https://relay.hawkxdev.dev/ping >/dev/null
curl -fsS http://127.0.0.1:8420/health >/dev/null
ufw status verbose
```

Ports 8431 through 8435 must be free. The existing DNS record, certificate, ACME webroot, static pages, nginx instance, and firewall rules are reused. Do not run certbot and do not open new firewall ports.

## Create the service account and directories

Stop if the user already exists with a different home, shell, UID, or group.

```bash
set -euo pipefail
useradd --system --create-home \
  --home-dir /var/lib/google-workspace-mcp \
  --shell /usr/sbin/nologin googlemcp
chown googlemcp:googlemcp /var/lib/google-workspace-mcp
chmod 0700 /var/lib/google-workspace-mcp
install -d -o root -g root -m 0755 \
  /opt/google-workspace-mcp/app \
  /opt/google-workspace-mcp/python
install -d -o root -g root -m 0700 /etc/google-mcp
for service in gmail calendar drive sheets docs; do
  install -d -o googlemcp -g googlemcp -m 0700 \
    "/var/lib/google-workspace-mcp/$service" \
    "/var/lib/google-workspace-mcp/$service/downloads" \
    "/var/log/google-workspace-mcp/$service"
done
```

Do not recreate or change `/opt/google-workspace-mcp/public`.

## Deliver the committed application

Deploy an owner approved commit, not the working tree. Record its full SHA before delivery. Use the SSH alias `google-mcp-host`, configured outside the repository for the managed host.

```bash
set -euo pipefail
REVISION=$(git rev-parse HEAD)
PAYLOAD=$(mktemp -d)
trap 'rm -rf "$PAYLOAD" "$PAYLOAD.sha256"' EXIT
git archive "$REVISION" \
  .python-version LICENSE NOTICE README.md deploy pyproject.toml uv.lock src \
  | tar -x -C "$PAYLOAD"
(
  cd "$PAYLOAD"
  find . -type f -print0 \
    | sort -z \
    | xargs -0 shasum -a 256
) > "$PAYLOAD.sha256"
tar -C "$PAYLOAD" -cf - . \
  | ssh google-mcp-host \
      'tar -xf - -C /opt/google-workspace-mcp/app'
scp "$PAYLOAD.sha256" \
  google-mcp-host:/root/google-mcp-payload.sha256
ssh google-mcp-host \
  'cd /opt/google-workspace-mcp/app && \
   sha256sum -c /root/google-mcp-payload.sha256 && \
   rm -f /root/google-mcp-payload.sha256'
```

Use a separate SSH call for activation. Do not combine a piped payload with a here document because both require SSH standard input. Do not use `rsync --delete` on `/opt/google-workspace-mcp`.

On the host, enforce the read only source boundary and install Python from the project directory:

```bash
set -euo pipefail
chown -R root:root /opt/google-workspace-mcp/app
find /opt/google-workspace-mcp/app -type d -exec chmod 0755 {} +
find /opt/google-workspace-mcp/app -type f -exec chmod 0644 {} +
cd /opt/google-workspace-mcp/app
export UV_PYTHON_INSTALL_DIR=/opt/google-workspace-mcp/python
/usr/local/bin/uv python install 3.14
/usr/local/bin/uv sync --no-dev --frozen
chown -R root:root \
  /opt/google-workspace-mcp/app \
  /opt/google-workspace-mcp/python
```

Before enabling a unit, prove that the service user can execute every entry point:

```bash
set -euo pipefail
for service in gmail calendar drive sheets docs; do
  sudo -u googlemcp test -x \
    "/opt/google-workspace-mcp/app/.venv/bin/google-mcp-$service"
done
```

## Install service configuration

Copy each tracked example to its matching environment file, then set only that service OAuth login username and password on the host. Never put live values in the repository or command output.

```bash
set -euo pipefail
for service in gmail calendar drive sheets docs; do
  install -o root -g root -m 0600 \
    "deploy/env/$service.env.example" \
    "/etc/google-mcp/$service.env"
done
```

Every environment file contains one uppercase service prefix. The public URL, port, MCP path, state path, Google token path, audit path, and download path must remain service specific.

## Deliver existing Google credentials

Use the five credentials already authorized for the exact service scope sets. Do not run `google-mcp-authorize` on the production host. Do not transfer credential lock files.

For each service, install the credential as:

```text
/var/lib/google-workspace-mcp/<service>/google_token.json
owner: googlemcp:googlemcp
mode: 0600
```

Verify source and destination SHA-256 values through aggregate output that does not include file contents. Verify five files, five owner and mode results, and the exact service scopes without printing token values.

## Install and start systemd instances

```bash
set -euo pipefail
install -o root -g root -m 0644 \
  deploy/google-mcp@.service \
  /etc/systemd/system/google-mcp@.service
systemctl daemon-reload
for service in gmail calendar drive sheets docs; do
  systemctl enable --now "google-mcp@$service"
done
```

A green systemd state is not acceptance. Check both liveness observations for every service:

```bash
set -euo pipefail
curl -fsS http://127.0.0.1:8431/health
curl -fsS http://127.0.0.1:8432/health
curl -fsS http://127.0.0.1:8433/health
curl -fsS http://127.0.0.1:8434/health
curl -fsS http://127.0.0.1:8435/health
ss -tlnp
```

Each health response must name its service, and each loopback port must belong to a `googlemcp` process.

## Install nginx configuration

Production uses a modular nginx layout separating static assets, upstreams, active service routing, and maintenance mode:

- `deploy/nginx-google-workspace-mcp.conf`: primary vhost configuration, including `/etc/nginx/snippets/google-workspace-mcp-dynamic.inc`;
- `deploy/nginx-google-workspace-mcp-active.inc`: active routing snippet proxying to loopback services, path-scoped OAuth routes, metadata discovery, and prefixed health/readiness;
- `deploy/nginx-google-workspace-mcp-maintenance.inc`: maintenance snippet returning `503` with `Retry-After: 300` for all service routes during cutover windows;
- `deploy/nginx-google-workspace-mcp-candidate.conf`: loopback-only TLS candidate ingress listening on `127.0.0.1:9443 ssl` for pre-cutover validation using production domain authority;
- `deploy/nginx-google-workspace-mcp-bootstrap.conf`: bootstrap HTTP port 80 configuration for DNS and ACME validation.

Install the dynamic snippet and primary vhost:

```bash
set -euo pipefail
install -d -o root -g root -m 0755 /etc/nginx/snippets
install -o root -g root -m 0644 \
  deploy/nginx-google-workspace-mcp-active.inc \
  /etc/nginx/snippets/google-workspace-mcp-dynamic.inc
sed 's/__DOMAIN__/mcp.hawkxdev.dev/g' \
  deploy/nginx-google-workspace-mcp.conf \
  > /tmp/google-workspace-mcp.conf
test "$(grep -c '__DOMAIN__' /tmp/google-workspace-mcp.conf)" -eq 0
BACKUP=/root/backups/google-workspace-mcp-$(date -u +%Y%m%dT%H%M%SZ)
install -d -o root -g root -m 0700 "$BACKUP"
printf '%s\n' "$BACKUP" > /root/backups/google-workspace-mcp-last-backup
chmod 0600 /root/backups/google-workspace-mcp-last-backup
if [[ -f /etc/nginx/sites-available/google-workspace-mcp.conf ]]; then
  cp -a \
    /etc/nginx/sites-available/google-workspace-mcp.conf \
    "$BACKUP/google-workspace-mcp.conf"
fi
install -o root -g root -m 0644 \
  /tmp/google-workspace-mcp.conf \
  /etc/nginx/sites-available/google-workspace-mcp.conf
if nginx -t; then
  systemctl reload nginx
else
  if [[ -f "$BACKUP/google-workspace-mcp.conf" ]]; then
    cp -a \
      "$BACKUP/google-workspace-mcp.conf" \
      /etc/nginx/sites-available/google-workspace-mcp.conf
  fi
  nginx -t
  exit 1
fi
```

The configuration preserves the homepage, privacy page, assets, ACME path, certificate, and fallback 404. It adds five loopback upstreams, path scoped OAuth and MCP routes, metadata routes, and prefixed health and readiness routes.

## Cutover safety CLI and ingress verification

The `google-mcp-cutover` CLI enforces safety gates during cutover and maintenance operations:

1. Identity preview: inspect and validate service identities and environment configuration:
   ```bash
   uv run --no-sync google-mcp-cutover identity preview --env-dir /etc/google-mcp
   ```
2. Reset preview: generate a state reset manifest before destructive transitions:
   ```bash
   uv run --no-sync google-mcp-cutover reset preview --env-dir /etc/google-mcp --state-root /var/lib/google-workspace-mcp
   ```
3. Maintenance attestation: verify nginx maintenance mode before mutating state:
   ```bash
   uv run --no-sync google-mcp-cutover maintenance attest \
     --identity-manifest identity.json \
     --output maintenance-attestation.json \
     --nginx-master-pid <pid> \
     --nginx-config-digest <sha256> \
     --maintenance-include-target /etc/nginx/snippets/google-workspace-mcp-dynamic.inc \
     --worker-generation <gen>
   ```
4. Offline snapshots: create and verify verified state snapshots before changes:
   ```bash
   uv run --no-sync google-mcp-cutover snapshot create \
     --identity-manifest identity.json \
     --reset-manifest reset.json \
     --maintenance-attestation maintenance-attestation.json \
     --destination /root/backups/google-mcp-snapshot
   uv run --no-sync google-mcp-cutover snapshot verify --manifest /root/backups/google-mcp-snapshot/manifest.json
   ```
5. Gate journaling: track irreversible cutover gates:
   ```bash
   uv run --no-sync google-mcp-cutover journal create \
     --identity-manifest identity.json \
     --reset-manifest reset.json \
     --snapshot-manifest snapshot.json \
     --maintenance-attestation maintenance-attestation.json \
     --output journal.json
   uv run --no-sync google-mcp-cutover journal mark-gate-opened --journal journal.json --confirm-sha256 <digest>
   ```

Ingress verification is automated via `deploy/check-cutover-ingress.sh`:

```bash
# Pre-cutover validation against loopback candidate port 9443:
./deploy/check-cutover-ingress.sh candidate

# Post-cutover validation against public HTTPS:
./deploy/check-cutover-ingress.sh public
```

The verification script confirms the positive 5-service matrix, rejects the negative routing matrix (old PRM, root OAuth, unmapped aliases), and asserts zero downstream state mutation during testing.

## Acceptance boundary

This deployment accepts infrastructure only. It does not register or authorize downstream MCP clients.

For each service verify:

- public authorization server metadata (`/.well-known/oauth-authorization-server/<service>`) returns `200` with the service-base issuer and endpoints under `/<service>/oauth/`;
- public protected resource metadata (`/.well-known/oauth-protected-resource/<service>/mcp`) returns `200` with canonical resource `https://mcp.hawkxdev.dev/<service>/mcp` and authorization server `https://mcp.hawkxdev.dev/<service>`;
- unauthenticated MCP `POST` to `/<service>/mcp` returns `401` with a Bearer challenge referencing `/.well-known/oauth-protected-resource/<service>/mcp`;
- public prefixed health (`/<service>/health`) returns `200` and names the service;
- public prefixed readiness (`/<service>/ready`) without a bearer token returns `401`;
- an authorization request without a registered client returns an OAuth `400`, proving routing without creating client state;
- old PRM route (`/.well-known/oauth-protected-resource/<service>`) and root OAuth routes (`/oauth/authorize`, `/oauth/token`, `/oauth/register`) return `404`.

Also verify HTTP to HTTPS redirect, HTTP/2, homepage, privacy page, static CSS, unknown path `404`, nginx syntax, unchanged firewall rules, unchanged certificate, `vault-mcp` health without a restart, `alert-relay` ping, and existing containers.

## Rollback boundary

Rollback must coordinate the application source, five environment files, five OAuth state databases, five Google credentials, systemd unit, nginx configuration, and public tree checksum. Never restore an older credential or OAuth state over newer production state without an explicit owner decision. Stop all five services before restoring state. Restore nginx only after `nginx -t`, then reload rather than restart.

