#!/usr/bin/env bash
# =============================================================================
# test_companion_api.sh — Smoke-test the companion REST + SSE endpoints
#
# Usage:
#   ./scripts/test_companion_api.sh                    # defaults
#   ./scripts/test_companion_api.sh -H 192.168.1.10    # custom host
#   ./scripts/test_companion_api.sh -p 9000            # custom port
#   ./scripts/test_companion_api.sh -k <api-key>       # use API key instead of JWT
#   ./scripts/test_companion_api.sh -P <pub_key_hex>   # target contact for send tests
#
# Requires: curl, jq
# =============================================================================

set -euo pipefail

# ----- Defaults -----
HOST="localhost"
PORT="8000"
USERNAME="admin"
PASSWORD=""
CLIENT_ID="test-companion-api"
API_KEY=""
TARGET_PUBKEY=""
COMPANION_NAME=""

# ----- Parse args -----
while getopts "H:p:u:w:k:P:c:h" opt; do
    case $opt in
        H) HOST="$OPTARG" ;;
        p) PORT="$OPTARG" ;;
        u) USERNAME="$OPTARG" ;;
        w) PASSWORD="$OPTARG" ;;
        k) API_KEY="$OPTARG" ;;
        P) TARGET_PUBKEY="$OPTARG" ;;
        c) COMPANION_NAME="$OPTARG" ;;
        h)
            echo "Usage: $0 [-H host] [-p port] [-u user] [-w password] [-k api_key] [-P target_pubkey] [-c companion_name]"
            exit 0
            ;;
        *) echo "Unknown option: -$opt" >&2; exit 1 ;;
    esac
done

BASE="http://${HOST}:${PORT}"
PASS=0
FAIL=0
SKIP=0

# ----- Colours -----
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ----- Helpers -----

auth_header() {
    if [[ -n "$API_KEY" ]]; then
        echo "X-API-Key: ${API_KEY}"
    elif [[ -n "$TOKEN" ]]; then
        echo "Authorization: Bearer ${TOKEN}"
    else
        echo ""
    fi
}

# Run a test: name, expected_http_code, curl_args...
run_test() {
    local name="$1"
    local expect_code="$2"
    shift 2

    printf "  %-50s " "$name"

    local tmpfile
    tmpfile=$(mktemp)

    local http_code
    http_code=$(curl -s -o "$tmpfile" -w "%{http_code}" \
        -H "$(auth_header)" \
        -H "Content-Type: application/json" \
        "$@" 2>/dev/null) || true

    local body
    body=$(cat "$tmpfile")
    rm -f "$tmpfile"

    if [[ "$http_code" == "$expect_code" ]]; then
        local success
        success=$(echo "$body" | jq -r '.success // empty' 2>/dev/null) || true
        if [[ "$success" == "true" ]]; then
            printf "${GREEN}PASS${NC}  (HTTP %s)\n" "$http_code"
            PASS=$((PASS + 1))
        elif [[ "$success" == "false" ]]; then
            local err
            err=$(echo "$body" | jq -r '.error // .data.reason // "unknown"' 2>/dev/null) || true
            printf "${YELLOW}PASS${NC}  (HTTP %s, success=false: %s)\n" "$http_code" "$err"
            PASS=$((PASS + 1))
        else
            printf "${GREEN}PASS${NC}  (HTTP %s)\n" "$http_code"
            PASS=$((PASS + 1))
        fi
    else
        printf "${RED}FAIL${NC}  (expected HTTP %s, got %s)\n" "$expect_code" "$http_code"
        if [[ -n "$body" ]]; then
            echo "         $(echo "$body" | jq -c '.' 2>/dev/null || echo "$body" | head -c 200)"
        fi
        FAIL=$((FAIL + 1))
    fi
}

skip_test() {
    local name="$1"
    local reason="$2"
    printf "  %-50s ${YELLOW}SKIP${NC}  (%s)\n" "$name" "$reason"
    SKIP=$((SKIP + 1))
}

# Pretty-print a JSON response
show_response() {
    local name="$1"
    shift
    printf "\n${CYAN}--- %s ---${NC}\n" "$name"
    curl -s -H "$(auth_header)" -H "Content-Type: application/json" "$@" 2>/dev/null | jq '.' 2>/dev/null || echo "(no JSON)"
    echo ""
}

# =============================================================================
# 0. Connectivity check
# =============================================================================

echo ""
echo "========================================"
echo " Companion API Test Suite"
echo " Target: ${BASE}"
echo "========================================"
echo ""

printf "Checking connectivity... "
if ! curl -sf -o /dev/null --connect-timeout 3 "${BASE}/api/needs_setup" 2>/dev/null; then
    printf "${RED}FAILED${NC}\n"
    echo "Cannot reach ${BASE}. Is the repeater running?"
    exit 1
fi
printf "${GREEN}OK${NC}\n"

# =============================================================================
# 1. Authentication
# =============================================================================

TOKEN=""

if [[ -n "$API_KEY" ]]; then
    echo ""
    echo "Using API key for authentication."
    TOKEN=""
elif [[ -n "$PASSWORD" ]]; then
    echo ""
    printf "Authenticating as '${USERNAME}'... "
    LOGIN_RESP=$(curl -s -X POST "${BASE}/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\",\"client_id\":\"${CLIENT_ID}\"}" 2>/dev/null)

    TOKEN=$(echo "$LOGIN_RESP" | jq -r '.token // empty' 2>/dev/null) || true
    if [[ -n "$TOKEN" ]]; then
        printf "${GREEN}OK${NC} (token received)\n"
    else
        printf "${RED}FAILED${NC}\n"
        echo "$LOGIN_RESP" | jq '.' 2>/dev/null || echo "$LOGIN_RESP"
        echo ""
        echo "Cannot authenticate. Provide -w <password> or -k <api_key>."
        exit 1
    fi
else
    echo ""
    echo "No password (-w) or API key (-k) provided."
    echo "Attempting unauthenticated requests (will fail if auth is required)."
    echo ""
fi

# =============================================================================
# 2. Read-only GET endpoints
# =============================================================================

echo ""
echo "--- GET endpoints (read-only) ---"

# Build companion_name query string if provided
QS=""
if [[ -n "$COMPANION_NAME" ]]; then
    QS="?companion_name=${COMPANION_NAME}"
fi

run_test "GET /api/companion/"               200 "${BASE}/api/companion/"
run_test "GET /api/companion/self_info"       200 "${BASE}/api/companion/self_info${QS}"
run_test "GET /api/companion/contacts"        200 "${BASE}/api/companion/contacts${QS}"
run_test "GET /api/companion/channels"        200 "${BASE}/api/companion/channels${QS}"
run_test "GET /api/companion/stats"           200 "${BASE}/api/companion/stats${QS}"
run_test "GET /api/companion/stats?type=core" 200 "${BASE}/api/companion/stats${QS:+${QS}&}${QS:+type=core}${QS:-?type=core}"

# Single contact lookup (needs a pub_key — grab the first one from contacts list)
FIRST_PUBKEY=$(curl -s -H "$(auth_header)" "${BASE}/api/companion/contacts${QS}" 2>/dev/null \
    | jq -r '.data[0].public_key // empty' 2>/dev/null) || true

if [[ -n "$FIRST_PUBKEY" ]]; then
    run_test "GET /api/companion/contact?pub_key=..." 200 \
        "${BASE}/api/companion/contact?pub_key=${FIRST_PUBKEY}${QS:+&companion_name=${COMPANION_NAME}}"
else
    skip_test "GET /api/companion/contact?pub_key=..." "no contacts available"
fi

# =============================================================================
# 3. Validation / error handling
# =============================================================================

echo ""
echo "--- Validation & error handling ---"

run_test "GET  /api/companion/contact (no pub_key)"   400 "${BASE}/api/companion/contact"
run_test "GET  /api/companion/contact (bad pub_key)"   400 "${BASE}/api/companion/contact?pub_key=zzzz"
run_test "POST send_text empty body"                   400 -X POST "${BASE}/api/companion/send_text" -d '{}'
run_test "GET  send_text (wrong method)"               405 "${BASE}/api/companion/send_text"

# =============================================================================
# 4. POST endpoints (write / send operations)
# =============================================================================

echo ""
echo "--- POST endpoints ---"

# Use TARGET_PUBKEY if provided, else use FIRST_PUBKEY from contacts list
PK="${TARGET_PUBKEY:-$FIRST_PUBKEY}"

if [[ -z "$PK" ]]; then
    skip_test "POST /api/companion/login"              "no target pubkey (-P)"
    skip_test "POST /api/companion/request_status"     "no target pubkey (-P)"
    skip_test "POST /api/companion/request_telemetry"  "no target pubkey (-P)"
    skip_test "POST /api/companion/send_text"          "no target pubkey (-P)"
    skip_test "POST /api/companion/send_command"       "no target pubkey (-P)"
    skip_test "POST /api/companion/reset_path"         "no target pubkey (-P)"
else
    # Build optional companion_name field for POST body
    CN_FIELD=""
    if [[ -n "$COMPANION_NAME" ]]; then
        CN_FIELD="\"companion_name\":\"${COMPANION_NAME}\","
    fi

    # Login (passwordless)
    run_test "POST /api/companion/login" 200 \
        -X POST "${BASE}/api/companion/login" \
        -d "{${CN_FIELD}\"pub_key\":\"${PK}\",\"password\":\"\"}"

    # Status request (may timeout — that's OK, we test the plumbing)
    run_test "POST /api/companion/request_status" 200 \
        -X POST "${BASE}/api/companion/request_status" \
        -d "{${CN_FIELD}\"pub_key\":\"${PK}\",\"timeout\":5}"

    # Telemetry request
    run_test "POST /api/companion/request_telemetry" 200 \
        -X POST "${BASE}/api/companion/request_telemetry" \
        -d "{${CN_FIELD}\"pub_key\":\"${PK}\",\"timeout\":5}"

    # Send text
    run_test "POST /api/companion/send_text" 200 \
        -X POST "${BASE}/api/companion/send_text" \
        -d "{${CN_FIELD}\"pub_key\":\"${PK}\",\"text\":\"API test ping\"}"

    # Send command
    run_test "POST /api/companion/send_command" 200 \
        -X POST "${BASE}/api/companion/send_command" \
        -d "{${CN_FIELD}\"pub_key\":\"${PK}\",\"command\":\"status\"}"

    # Reset path
    run_test "POST /api/companion/reset_path" 200 \
        -X POST "${BASE}/api/companion/reset_path" \
        -d "{${CN_FIELD}\"pub_key\":\"${PK}\"}"
fi

# =============================================================================
# 5. Device configuration endpoints
# =============================================================================

echo ""
echo "--- Device configuration ---"

CN_FIELD=""
if [[ -n "$COMPANION_NAME" ]]; then
    CN_FIELD="\"companion_name\":\"${COMPANION_NAME}\","
fi

# Set advert name (we'll read it back to verify)
run_test "POST /api/companion/set_advert_name" 200 \
    -X POST "${BASE}/api/companion/set_advert_name" \
    -d "{${CN_FIELD}\"advert_name\":\"TestNode\"}"

run_test "POST /api/companion/set_advert_location" 200 \
    -X POST "${BASE}/api/companion/set_advert_location" \
    -d "{${CN_FIELD}\"latitude\":37.7749,\"longitude\":-122.4194}"

# =============================================================================
# 6. SSE event stream (quick connect/disconnect test)
# =============================================================================

echo ""
echo "--- SSE event stream ---"

SSE_URL="${BASE}/api/companion/events"
if [[ -n "$TOKEN" ]]; then
    SSE_URL="${SSE_URL}?token=${TOKEN}"
elif [[ -n "$API_KEY" ]]; then
    # SSE via EventSource doesn't support custom headers; API key in query not supported
    # so we just test that the endpoint responds
    SSE_URL="${SSE_URL}"
fi

printf "  %-50s " "SSE /api/companion/events (3s sample)"

SSE_TMP=$(mktemp)
# Connect for 3 seconds, capture whatever comes
curl -s -N --max-time 3 \
    -H "$(auth_header)" \
    "$SSE_URL" > "$SSE_TMP" 2>/dev/null || true

SSE_LINES=$(wc -l < "$SSE_TMP" | tr -d ' ')
SSE_FIRST=$(head -1 "$SSE_TMP")

if [[ "$SSE_LINES" -gt 0 ]] && echo "$SSE_FIRST" | grep -q "data:"; then
    # Check for connected event
    if grep -q '"connected"' "$SSE_TMP" 2>/dev/null; then
        printf "${GREEN}PASS${NC}  (connected event received, %s lines)\n" "$SSE_LINES"
        PASS=$((PASS + 1))
    else
        printf "${YELLOW}PASS${NC}  (got %s lines, no 'connected' event)\n" "$SSE_LINES"
        PASS=$((PASS + 1))
    fi
else
    printf "${RED}FAIL${NC}  (no SSE data received)\n"
    FAIL=$((FAIL + 1))
fi
rm -f "$SSE_TMP"

# =============================================================================
# 7. Verbose output: show full response bodies
# =============================================================================

echo ""
echo "--- Sample responses ---"

show_response "Companion listing" "${BASE}/api/companion/"
show_response "Self info"         "${BASE}/api/companion/self_info${QS}"
show_response "Contacts"          "${BASE}/api/companion/contacts${QS}"
show_response "Stats (packets)"   "${BASE}/api/companion/stats${QS}"

# =============================================================================
# Summary
# =============================================================================

echo ""
echo "========================================"
printf " Results:  ${GREEN}%d passed${NC}" "$PASS"
if [[ "$FAIL" -gt 0 ]]; then
    printf ",  ${RED}%d failed${NC}" "$FAIL"
fi
if [[ "$SKIP" -gt 0 ]]; then
    printf ",  ${YELLOW}%d skipped${NC}" "$SKIP"
fi
echo ""
echo "========================================"
echo ""

exit $FAIL
