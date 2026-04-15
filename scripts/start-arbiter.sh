#!/usr/bin/env bash
# ARBITER — Production startup script
# Ensures env is valid, runs migrations, then starts the server + workers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# ─── Colours ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}[start]${NC} $*"; }
warn() { echo -e "${YELLOW}[start]${NC} WARNING: $*" >&2; }
die()  { echo -e "${RED}[start]${NC} ERROR: $*" >&2; exit 1; }

# ─── Check env ─────────────────────────────────────────────────────────
log "Validating environment..."
python3 scripts/migrate.py --check-env || die "Environment validation failed"

# ─── DRY_RUN guard ────────────────────────────────────────────────────
if [[ "${DRY_RUN:-true}" != "false" ]]; then
  log "DRY_RUN=enabled — safe to start"
elif [[ -z "${KALSHI_API_KEY_ID:-}" ]]; then
  die "DRY_RUN=false but KALSHI_API_KEY_ID not set — aborting"
else
  warn "DRY_RUN=disabled — LIVE TRADING ACTIVE"
fi

# ─── Migrations ───────────────────────────────────────────────────────
log "Running database migrations..."
python3 scripts/migrate.py --apply || die "Migrations failed"

# ─── PID file ─────────────────────────────────────────────────────────
PID_FILE="${PID_FILE:-/tmp/arbiter-server.pid}"

cleanup() {
  log "Shutting down..."
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
}
trap cleanup EXIT INT TERM

# ─── Start API server ─────────────────────────────────────────────────
log "Starting API server on port ${ARBITER_PORT:-8090}..."
CMD=(python3 -m arbiter.main --port "${ARBITER_PORT:-8090}" --log-level "${LOG_LEVEL:-INFO}")
if [[ "${DRY_RUN:-true}" == "false" ]]; then
  CMD+=(--live)
fi
"${CMD[@]}" > /tmp/arbiter-server.log 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > "$PID_FILE"
log "API server started (PID=$SERVER_PID)"

log "ARBITER is running. Logs: /tmp/arbiter-server.log"
log "API: http://127.0.0.1:${ARBITER_PORT:-8090}"
log "Dashboard: http://127.0.0.1:${ARBITER_PORT:-8090}/ops"
log "Health: http://127.0.0.1:${ARBITER_PORT:-8090}/api/health"

wait
