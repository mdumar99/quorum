#!/usr/bin/env bash
# Smoke test: proves Phase 0's Definition of Done end to end.
# Brings up the full stack under its OWN Compose project, checks the API and
# the web page, and ALWAYS tears everything down (containers and volumes).
#
# Usage: bash scripts/smoke-test.sh     (needs a .env: cp .env.example .env)
set -euo pipefail

PROJECT="quorum-smoke"                              # isolated from the dev stack ("quorum")
WAIT_TIMEOUT="${SMOKE_WAIT_TIMEOUT:-180}"           # seconds; overridable for testing
API_HEALTH_URL="http://localhost:8000/health"
WEB_URL="http://localhost:3000/"
EXPECTED_HEALTH='{"status":"ok","db":"ok","redis":"ok"}'   # the P0-3 contract, exactly

cd "$(dirname "$0")/.."                             # always run from the repo root

compose() { docker compose -p "$PROJECT" "$@"; }
log()     { printf '\n==> %s\n' "$*"; }
fail()    { printf '\n❌ SMOKE TEST FAILED: %s\n' "$*" >&2; exit 1; }

cleanup() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    log "Failure: service status"
    compose ps -a || true
    log "Failure: last 30 log lines from every service"
    compose logs --tail 30 || true
  fi
  log "Tearing down (containers and volumes)"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$code"
}

# ---------- preflight: fail fast with a clear message ----------
[ -f .env ] || fail ".env not found. Run: cp .env.example .env"

# Read the Postgres host port from .env without executing the file.
PG_PORT="$(grep -E '^POSTGRES_HOST_PORT=' .env | cut -d= -f2 || true)"
PG_PORT="${PG_PORT:-5432}"

# -p isolates containers and volumes, NOT host ports. Detect a running dev stack.
for port in 8000 3000 "$PG_PORT" 6379; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    fail "port $port is already in use. Stop the dev stack first: docker compose down"
  fi
done

# Registered only now: if preflight fails, nothing was started, so there's nothing to tear down.
trap cleanup EXIT

# ---------- bring the stack up ----------
log "Starting the stack (waiting up to ${WAIT_TIMEOUT}s for all services to be healthy)"
compose up -d --build --wait --wait-timeout "$WAIT_TIMEOUT" \
  || fail "services did not become healthy within ${WAIT_TIMEOUT}s"

# ---------- check the API ----------
log "Checking API: $API_HEALTH_URL"
body="$(curl -fsS --max-time 5 "$API_HEALTH_URL")" || fail "API /health did not return 2xx"
[ "$body" = "$EXPECTED_HEALTH" ] || fail "unexpected /health body: $body"

# ---------- check the web page ----------
log "Checking web: $WEB_URL"
page="$(curl -fsS --max-time 10 "$WEB_URL")" || fail "web page did not return 2xx"
grep -q 'data-testid="health-status"' <<<"$page" || fail "health-status marker missing from page"
grep -q 'data-state="healthy"' <<<"$page"        || fail "page is not in the healthy state"

log "✅ Smoke test passed"
