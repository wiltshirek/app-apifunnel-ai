#!/usr/bin/env bash
# test-endpoints.sh — smoke + happy-path harness for the subagents service.
#
# Exercises:
#   POST /v1/subagents                                     (legacy alias)
#   POST /v1/subagents/headless_reasoning_action_with_tools (canonical)
#   POST /v1/subagents/one-shot                            (embedded LLM primitive)
#
# Usage:
#   ./services/subagents/scripts/test-endpoints.sh [--host URL] [--verbose]
#
# Env vars (optional — if unset, happy-path tests skip):
#   OPENAI_API_KEY       — for the one-shot OpenAI happy-path test
#   ANTHROPIC_API_KEY    — for the one-shot Anthropic happy-path test
#   GOOGLE_API_KEY       — for the one-shot Google happy-path test
#
# Env vars (required; auto-loaded from repo .env):
#   MCP_ADMIN_KEY        — service admin key
#
# See project_overview/TESTING.md for full setup instructions.

set -u

HOST="${HOST:-http://localhost:3001}"
VERBOSE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --verbose|-v) VERBOSE=1; shift ;;
        --help|-h)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

# Resolve repo root — the .env lives two dirs up from this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "❌ cannot find $ENV_FILE"
    exit 2
fi

# Extract MCP_ADMIN_KEY preserving trailing '='
ADMIN=$(grep -E '^MCP_ADMIN_KEY=' "$ENV_FILE" | sed 's/^MCP_ADMIN_KEY=//')
if [[ -z "$ADMIN" ]]; then
    echo "❌ MCP_ADMIN_KEY not set in $ENV_FILE"
    exit 2
fi

# ── JWT mint (unsigned — server decodes without verifying signature) ─────────
mint_jwt() {
    local payload_json="$1"
    local header
    local payload
    header=$(printf '%s' '{"alg":"HS256","typ":"JWT"}' | base64 | tr -d '=' | tr '/+' '_-')
    payload=$(printf '%s' "$payload_json" | base64 | tr -d '=' | tr '/+' '_-')
    printf '%s.%s.fakesig1234567890abcdef' "$header" "$payload"
}

# ── Test runner plumbing ─────────────────────────────────────────────────────
PASS=0
FAIL=0
SKIP=0
FAILURES=()

assert_code() {
    local label="$1"
    local want="$2"
    local got="$3"
    local body="$4"

    if [[ "$got" == "$want" ]]; then
        PASS=$((PASS + 1))
        printf "  ✅ %-55s HTTP %s\n" "$label" "$got"
    else
        FAIL=$((FAIL + 1))
        FAILURES+=("$label (want $want, got $got)")
        printf "  ❌ %-55s HTTP %s (want %s)\n" "$label" "$got" "$want"
        if [[ -n "$body" ]]; then
            printf "     body: %s\n" "$(printf '%s' "$body" | head -c 200)"
        fi
    fi
}

assert_json_field() {
    local label="$1"
    local body="$2"
    local field="$3"
    local want_nonempty="${4:-1}"

    local val
    val=$(printf '%s' "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    v = d
    for k in '$field'.split('.'):
        v = v.get(k) if isinstance(v, dict) else None
    print('' if v is None else v)
except Exception:
    print('__ERR__')
" 2>/dev/null)

    if [[ "$val" == "__ERR__" ]]; then
        FAIL=$((FAIL + 1))
        FAILURES+=("$label (body not JSON)")
        printf "  ❌ %-55s body not JSON\n" "$label"
        return
    fi

    if [[ "$want_nonempty" == "1" && -z "$val" ]]; then
        FAIL=$((FAIL + 1))
        FAILURES+=("$label (field '$field' empty)")
        printf "  ❌ %-55s field %s is empty\n" "$label" "$field"
        return
    fi

    PASS=$((PASS + 1))
    printf "  ✅ %-55s %s=%s\n" "$label" "$field" "$(printf '%s' "$val" | head -c 50)"
}

skip() {
    SKIP=$((SKIP + 1))
    printf "  ⊘  %-55s %s\n" "$1" "(${2:-skipped})"
}

call() {
    local method="$1"
    local path="$2"
    shift 2
    local tmp
    tmp=$(mktemp)
    local code
    code=$(curl -s -m 60 -o "$tmp" -w "%{http_code}" -X "$method" "${HOST}${path}" "$@")
    local body
    body=$(cat "$tmp")
    rm -f "$tmp"
    if [[ $VERBOSE == 1 ]]; then
        printf "     → %s %s → %s\n" "$method" "$path" "$code"
        printf "     response: %s\n" "$(printf '%s' "$body" | head -c 300)"
    fi
    printf '%s\n%s' "$code" "$body"
}

echo "════════════════════════════════════════════════════════════"
echo "  Subagents endpoint harness"
echo "════════════════════════════════════════════════════════════"
echo "  Host:        $HOST"
echo "  Admin key:   ***${ADMIN: -4}"
echo ""

# ── Health ───────────────────────────────────────────────────────────────────
echo "── Health ──"
R=$(call GET /health)
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "GET /health" 200 "$CODE" "$BODY"

R=$(call GET /v1/openapi.yaml)
CODE=$(printf '%s' "$R" | head -1)
assert_code "GET /v1/openapi.yaml" 200 "$CODE" ""

echo ""

# Build a minimal JWT (no api_settings) for negative-path tests.
JWT_MINIMAL=$(mint_jwt '{"sub":"user_harness","email":"harness@test.local"}')

# ── Negative paths — no API keys needed ─────────────────────────────────────
echo "── Negative paths (auth + validation) ──"

# one-shot: unauth
R=$(call POST /v1/subagents/one-shot -H "Content-Type: application/json" -d '{"prompt":"hi"}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST one-shot  [unauth]" 401 "$CODE" "$BODY"

# one-shot: empty body
R=$(call POST /v1/subagents/one-shot \
    -H "Authorization: Bearer $JWT_MINIMAL" \
    -H "X-Admin-Key: $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST one-shot  [empty body]" 400 "$CODE" "$BODY"

# one-shot: no api_settings in JWT → missing_api_key
R=$(call POST /v1/subagents/one-shot \
    -H "Authorization: Bearer $JWT_MINIMAL" \
    -H "X-Admin-Key: $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"hi"}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST one-shot  [no api_key]" 400 "$CODE" "$BODY"

# one-shot: wrong admin key
R=$(call POST /v1/subagents/one-shot \
    -H "Authorization: Bearer $JWT_MINIMAL" \
    -H "X-Admin-Key: wrong-admin-key" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"hi"}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST one-shot  [wrong admin key]" 401 "$CODE" "$BODY"

# headless_reasoning_action_with_tools: unauth
R=$(call POST /v1/subagents/headless_reasoning_action_with_tools \
    -H "Content-Type: application/json" -d '{"message":"hi"}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST headless  [unauth]" 401 "$CODE" "$BODY"

# headless: empty body
R=$(call POST /v1/subagents/headless_reasoning_action_with_tools \
    -H "Authorization: Bearer $JWT_MINIMAL" \
    -H "X-Admin-Key: $ADMIN" \
    -H "Content-Type: application/json" -d '{}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST headless  [empty body]" 400 "$CODE" "$BODY"

# Legacy alias POST /v1/subagents — same handler as headless_*
R=$(call POST /v1/subagents \
    -H "Authorization: Bearer $JWT_MINIMAL" \
    -H "X-Admin-Key: $ADMIN" \
    -H "Content-Type: application/json" -d '{}')
CODE=$(printf '%s' "$R" | head -1)
BODY=$(printf '%s' "$R" | tail -n +2)
assert_code "POST /v1/subagents  [legacy alias, empty body]" 400 "$CODE" "$BODY"

# Check that /v1/subagents/one-shot did NOT accidentally match one of the dynamic
# routes (like /subagents/:id/message) — belt-and-suspenders.
R=$(call GET /v1/subagents/one-shot \
    -H "Authorization: Bearer $JWT_MINIMAL" \
    -H "X-Admin-Key: $ADMIN")
CODE=$(printf '%s' "$R" | head -1)
# "one-shot" has no GET handler, so 404 is correct (not 200, not matching :id route)
if [[ "$CODE" == "404" || "$CODE" == "405" || "$CODE" == "500" ]]; then
    PASS=$((PASS + 1))
    printf "  ✅ %-55s HTTP %s (non-200, no route collision)\n" "GET /v1/subagents/one-shot [verify no :id match]" "$CODE"
else
    FAIL=$((FAIL + 1))
    FAILURES+=("GET /v1/subagents/one-shot matched an unexpected route")
    printf "  ❌ %-55s HTTP %s (unexpected — route collision?)\n" "GET /v1/subagents/one-shot" "$CODE"
fi

echo ""

# ── Happy paths — require real LLM API key ─────────────────────────────────
echo "── Happy paths (real LLM calls) ──"

run_one_shot_happy() {
    local provider="$1"
    local model="$2"
    local key="$3"

    if [[ -z "$key" ]]; then
        local envvar
        envvar="$(printf '%s' "$provider" | tr 'a-z' 'A-Z')_API_KEY"
        skip "POST one-shot  [$provider happy path]" "set $envvar"
        return
    fi

    local payload_obj
    payload_obj=$(python3 -c "
import json
print(json.dumps({
    'sub': 'user_harness',
    'email': 'harness@test.local',
    'api_settings': { '$provider': { 'api_key': '$key' } }
}))
")
    local jwt
    jwt=$(mint_jwt "$payload_obj")

    local R
    R=$(call POST /v1/subagents/one-shot \
        -H "Authorization: Bearer $jwt" \
        -H "X-Admin-Key: $ADMIN" \
        -H "Content-Type: application/json" \
        -d "{\"prompt\":\"Respond with exactly the word: pong. No punctuation.\", \"model\":\"$model\", \"max_tokens\":10}")
    local code
    code=$(printf '%s' "$R" | head -1)
    local body
    body=$(printf '%s' "$R" | tail -n +2)

    if [[ "$code" == "200" ]]; then
        assert_json_field "POST one-shot  [$provider $model]" "$body" "text" 1
        assert_json_field "    └ tokens.total"                "$body" "tokens.total" 1
        assert_json_field "    └ provider"                    "$body" "provider" 1
    else
        FAIL=$((FAIL + 1))
        FAILURES+=("POST one-shot [$provider $model] (HTTP $code)")
        printf "  ❌ %-55s HTTP %s\n" "POST one-shot  [$provider $model]" "$code"
        printf "     body: %s\n" "$(printf '%s' "$body" | head -c 200)"
    fi
}

run_one_shot_happy "openai"    "gpt-4o-mini"             "${OPENAI_API_KEY:-}"
run_one_shot_happy "anthropic" "claude-haiku-4-5"        "${ANTHROPIC_API_KEY:-}"
run_one_shot_happy "google"    "gemini-flash-lite-latest" "${GOOGLE_API_KEY:-${GEMINI_API_KEY:-}}"

echo ""

# headless_react happy path: only attempt if APP_BASE_URL looks reachable + some LLM key exists.
if [[ -n "${OPENAI_API_KEY:-${ANTHROPIC_API_KEY:-}}" ]]; then
    APP_URL=$(grep -E '^APP_BASE_URL=' "$ENV_FILE" | sed 's/^APP_BASE_URL=//' || echo "")
    APP_URL="${APP_URL:-http://localhost:4000}"
    # Probe
    probe_code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "$APP_URL/health" 2>/dev/null || echo "000")
    if [[ "$probe_code" == "200" ]]; then
        echo "── Headless async dispatch (requires $APP_URL running) ──"
        kp=$(python3 -c "
import json
s={}
if '${OPENAI_API_KEY:-}': s['openai']={'api_key':'${OPENAI_API_KEY:-}'}
if '${ANTHROPIC_API_KEY:-}': s['anthropic']={'api_key':'${ANTHROPIC_API_KEY:-}'}
print(json.dumps({'sub':'user_harness','email':'harness@test.local','api_settings':s}))
")
        jwt_full=$(mint_jwt "$kp")

        R=$(call POST /v1/subagents/headless_reasoning_action_with_tools \
            -H "Authorization: Bearer $jwt_full" \
            -H "X-Admin-Key: $ADMIN" \
            -H "Content-Type: application/json" \
            -d '{"message":"Harness smoke: respond once with the word pong","max_turns":1}')
        code=$(printf '%s' "$R" | head -1)
        body=$(printf '%s' "$R" | tail -n +2)
        if [[ "$code" == "200" ]]; then
            assert_json_field "POST headless  [dispatched]" "$body" "task_id" 1
            assert_json_field "    └ status"                "$body" "status" 1
        else
            FAIL=$((FAIL + 1))
            FAILURES+=("POST headless dispatch HTTP $code")
            printf "  ❌ %-55s HTTP %s\n" "POST headless [dispatched]" "$code"
        fi
    else
        skip "POST headless  [happy path]" "$APP_URL not reachable"
    fi
else
    skip "POST headless  [happy path]" "no OPENAI/ANTHROPIC key"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
printf "  Results: %d passed, %d failed, %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "════════════════════════════════════════════════════════════"

if [[ $FAIL -gt 0 ]]; then
    echo ""
    echo "Failures:"
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
    exit 1
fi

exit 0
