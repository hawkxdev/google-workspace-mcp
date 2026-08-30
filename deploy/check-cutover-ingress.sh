#!/usr/bin/env bash
# Ingress verification suite for Hawkx Workspace MCP cutover.
# Validates candidate and public routing contracts across all five services.

set -euo pipefail

VERSION="1.0.0"
DOMAIN="mcp.hawkxdev.dev"
BASE_URL="https://${DOMAIN}"
CANDIDATE_ADDR="127.0.0.1:9443"
SERVICES=("gmail" "calendar" "drive" "sheets" "docs")
STATE_ROOT="${STATE_ROOT:-/var/lib/google-workspace-mcp}"

usage() {
    echo "Usage: $0 {candidate|public} [--version] [--help]"
    echo ""
    echo "Modes:"
    echo "  candidate  Probe candidate ingress on 127.0.0.1:9443 using curl --connect-to"
    echo "  public     Probe live public ingress on https://mcp.hawkxdev.dev"
    exit 1
}

if [[ $# -eq 0 ]]; then
    usage
fi

MODE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        candidate|public)
            if [[ -n "$MODE" ]]; then
                echo "Error: Multiple modes specified" >&2
                usage
            fi
            MODE="$1"
            shift
            ;;
        --version|-v)
            echo "check-cutover-ingress.sh version ${VERSION}"
            exit 0
            ;;
        --help|-h)
            echo "Usage: $0 {candidate|public} [--version] [--help]"
            echo ""
            echo "Modes:"
            echo "  candidate  Probe candidate ingress on 127.0.0.1:9443 using curl --connect-to"
            echo "  public     Probe live public ingress on https://mcp.hawkxdev.dev"
            exit 0
            ;;
        *)
            echo "Error: Unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [[ -z "$MODE" ]]; then
    usage
fi

CURL_EXTRA_ARGS=()
if [[ "$MODE" == "candidate" ]]; then
    CURL_EXTRA_ARGS=(--connect-to "${DOMAIN}:443:${CANDIDATE_ADDR}")
fi

TMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

count_oauth_rows() {
    local total=0
    if command -v sqlite3 >/dev/null 2>&1; then
        for svc in "${SERVICES[@]}"; do
            local db="${STATE_ROOT}/${svc}/oauth_state.sqlite3"
            if [[ -f "$db" && -r "$db" ]]; then
                for tbl in clients authorization_codes access_tokens refresh_tokens; do
                    local count
                    count="$(sqlite3 "$db" "SELECT COUNT(*) FROM ${tbl};" 2>/dev/null || echo 0)"
                    total=$((total + count))
                done
            fi
        done
    fi
    echo "$total"
}

INITIAL_ROW_COUNT="$(count_oauth_rows)"

probe() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    local content_type="${4:-}"
    local headers_file="${TMP_DIR}/headers.txt"
    local body_file="${TMP_DIR}/body.txt"

    rm -f "$headers_file" "$body_file"

    local curl_cmd=(
        curl
        --silent
        --show-error
        --max-time 15
        "${CURL_EXTRA_ARGS[@]}"
        -D "$headers_file"
        -o "$body_file"
        -w "%{http_code}"
    )

    if [[ "$method" == "HEAD" ]]; then
        curl_cmd+=(--head)
    else
        curl_cmd+=(-X "$method")
    fi

    if [[ -n "$content_type" ]]; then
        curl_cmd+=(-H "Content-Type: ${content_type}")
    fi

    if [[ -n "$data" ]]; then
        curl_cmd+=(--data "$data")
    fi

    curl_cmd+=("${BASE_URL}${path}")

    local status
    status="$("${curl_cmd[@]}")"
    echo "$status"
}

assert_no_redirect() {
    local headers_file="${TMP_DIR}/headers.txt"
    if [[ -f "$headers_file" ]]; then
        if grep -qi "^Location:" "$headers_file"; then
            echo "FAIL: Unexpected redirect header detected" >&2
            exit 1
        fi
    fi
}

echo "Running cutover ingress verification in ${MODE} mode..."

# 1. Five service positive identity assertions
for svc in "${SERVICES[@]}"; do
    echo "Probing positive identity for ${svc}..."

    # PRM
    prm_path="/.well-known/oauth-protected-resource/${svc}/mcp"
    status="$(probe "GET" "$prm_path")"
    if [[ "$status" != "200" ]]; then
        echo "FAIL: Expected 200 for PRM at ${prm_path}, got ${status}" >&2
        exit 1
    fi
    if ! grep -q "\"resource\"[[:space:]]*:[[:space:]]*\"https://${DOMAIN}/${svc}/mcp\"" "${TMP_DIR}/body.txt"; then
        echo "FAIL: PRM missing exact resource URL for ${svc}" >&2
        exit 1
    fi
    if ! grep -q "https://${DOMAIN}/${svc}" "${TMP_DIR}/body.txt"; then
        echo "FAIL: PRM missing issuer reference for ${svc}" >&2
        exit 1
    fi

    # AS metadata
    as_path="/.well-known/oauth-authorization-server/${svc}"
    status="$(probe "GET" "$as_path")"
    if [[ "$status" != "200" ]]; then
        echo "FAIL: Expected 200 for AS metadata at ${as_path}, got ${status}" >&2
        exit 1
    fi
    if ! grep -q "\"issuer\"[[:space:]]*:[[:space:]]*\"https://${DOMAIN}/${svc}\"" "${TMP_DIR}/body.txt"; then
        echo "FAIL: AS metadata missing exact issuer URL for ${svc}" >&2
        exit 1
    fi
    if ! grep -q "\"resource\"[[:space:]]*:[[:space:]]*\"https://${DOMAIN}/${svc}/mcp\"" "${TMP_DIR}/body.txt"; then
        echo "FAIL: AS metadata missing exact resource URL for ${svc}" >&2
        exit 1
    fi

    # Unauthenticated MCP challenge
    mcp_path="/${svc}/mcp"
    status="$(probe "POST" "$mcp_path" "" "application/json")"
    if [[ "$status" != "401" ]]; then
        echo "FAIL: Expected 401 unauthenticated challenge for MCP at ${mcp_path}, got ${status}" >&2
        exit 1
    fi
    if ! grep -qi "WWW-Authenticate:.*resource_metadata=\"https://${DOMAIN}/\.well-known/oauth-protected-resource/${svc}/mcp\"" "${TMP_DIR}/headers.txt"; then
        echo "FAIL: MCP WWW-Authenticate header missing exact resource_metadata for ${svc}" >&2
        exit 1
    fi

    # Health probe
    health_path="/${svc}/health"
    status="$(probe "GET" "$health_path")"
    if [[ "$status" != "200" ]]; then
        echo "FAIL: Expected 200 for health at ${health_path}, got ${status}" >&2
        exit 1
    fi

    # Ready probe
    ready_path="/${svc}/ready"
    status="$(probe "GET" "$ready_path")"
    if [[ "$status" != "401" ]]; then
        echo "FAIL: Expected 401 for ready at ${ready_path}, got ${status}" >&2
        exit 1
    fi

    # Non-mutating operational route probes
    # Malformed DCR payload rejects without state mutation
    dcr_path="/${svc}/oauth/register"
    status="$(probe "POST" "$dcr_path" "{\"invalid_json\": " "application/json")"
    if [[ "$status" != "400" ]]; then
        echo "FAIL: Expected 400 for malformed DCR at ${dcr_path}, got ${status}" >&2
        exit 1
    fi

    # Unknown client authorize rejects without code generation
    auth_path="/${svc}/oauth/authorize?client_id=nonexistent_probe_client&response_type=code"
    status="$(probe "GET" "$auth_path")"
    if [[ "$status" != "400" ]]; then
        echo "FAIL: Expected 400 for unknown client authorize at ${auth_path}, got ${status}" >&2
        exit 1
    fi

    # Unsupported grant token rejects without token generation
    token_path="/${svc}/oauth/token"
    status="$(probe "POST" "$token_path" "grant_type=unsupported_probe_grant&client_id=nonexistent" "application/x-www-form-urlencoded")"
    if [[ "$status" != "400" ]]; then
        echo "FAIL: Expected 400 for unsupported grant token at ${token_path}, got ${status}" >&2
        exit 1
    fi
done

# 2. Negative matrix for old PRM, root OAuth, and aliases across GET, HEAD, POST
NEGATIVE_PATHS=(
    "/register"
    "/authorize"
    "/token"
    "/oauth/register"
    "/oauth/authorize"
    "/oauth/token"
    "/.well-known/oauth-protected-resource"
    "/.well-known/oauth-protected-resource/"
    "/.well-known/oauth-authorization-server"
    "/.well-known/oauth-authorization-server/"
    "/.well-known/oauth-protected-resource/invalid-service/mcp"
    "/.well-known/oauth-authorization-server/invalid-service"
)

for svc in "${SERVICES[@]}"; do
    # Old service-base PRM must be closed
    NEGATIVE_PATHS+=("/.well-known/oauth-protected-resource/${svc}")
done

for method in "GET" "HEAD" "POST"; do
    for path in "${NEGATIVE_PATHS[@]}"; do
        status="$(probe "$method" "$path" "" "application/json")"
        assert_no_redirect
        if [[ "$status" =~ ^30[0-9]$ ]]; then
            echo "FAIL: Negative endpoint ${path} with method ${method} returned redirect ${status}" >&2
            exit 1
        fi
        if [[ "$status" != "404" && "$status" != "400" && "$status" != "401" && "$status" != "405" ]]; then
            echo "FAIL: Negative endpoint ${path} with method ${method} returned unexpected status ${status}" >&2
            exit 1
        fi
    done
done

# 3. Assert zero mutation in downstream OAuth databases during candidate run
FINAL_ROW_COUNT="$(count_oauth_rows)"
if [[ "$INITIAL_ROW_COUNT" -ne "$FINAL_ROW_COUNT" ]]; then
    echo "FAIL: OAuth state mutated during ingress check (initial=${INITIAL_ROW_COUNT}, final=${FINAL_ROW_COUNT})" >&2
    exit 1
fi

echo "Ingress verification passed in ${MODE} mode."
exit 0
