#!/usr/bin/env bash
# ARBITER — Production startup script
# Ensures env is valid, runs migrations, then starts the server + workers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${ARBITER_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "[start] ERROR: no Python interpreter found. Run ./scripts/setup/bootstrap_python.sh first." >&2
    exit 1
  fi
fi

cd "$ROOT_DIR"

ENV_FILE="${ARBITER_ENV_FILE:-}"
if [[ -z "$ENV_FILE" ]]; then
  if [[ -f "$ROOT_DIR/.env.production" ]]; then
    ENV_FILE="$ROOT_DIR/.env.production"
  elif [[ -f "$ROOT_DIR/.env" ]]; then
    ENV_FILE="$ROOT_DIR/.env"
  fi
fi

if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# ─── Colours ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}[start]${NC} $*"; }
warn() { echo -e "${YELLOW}[start]${NC} WARNING: $*" >&2; }
die()  { echo -e "${RED}[start]${NC} ERROR: $*" >&2; exit 1; }

# ─── Check env ─────────────────────────────────────────────────────────
log "Validating environment with $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))..."
"$PYTHON_BIN" scripts/migrate.py --check-env || die "Environment validation failed"

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
"$PYTHON_BIN" scripts/migrate.py --apply || die "Migrations failed"

# ─── Start API server ─────────────────────────────────────────────────
HOST="${ARBITER_HOST:-0.0.0.0}"
PORT="${ARBITER_PORT:-8090}"
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
PID_FILE="${PID_FILE:-/tmp/arbiter-server.pid}"
LOCK_DIR="${PID_FILE}.lock"
LOCK_OWNER_FILE="${LOCK_DIR}/owner.pid"
SERVER_PID=""

cleanup() {
  local exit_code=$?
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    log "Shutting down..."
    kill "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -f "$PID_FILE" ]]; then
    local recorded_pid
    recorded_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -z "$SERVER_PID" || "$recorded_pid" == "$SERVER_PID" ]]; then
      rm -f "$PID_FILE"
    fi
  fi
  rmdir "$LOCK_DIR" 2>/dev/null || true
  exit "$exit_code"
}

if [[ -d "$LOCK_DIR" ]]; then
  EXISTING_LOCK_PID="$(cat "$LOCK_OWNER_FILE" 2>/dev/null || true)"
  if [[ -n "$EXISTING_LOCK_PID" ]] && kill -0 "$EXISTING_LOCK_PID" 2>/dev/null; then
    die "Another start-arbiter invocation is already in progress (PID=$EXISTING_LOCK_PID)"
  fi
  warn "Removing stale startup lock: $LOCK_DIR"
  rm -rf "$LOCK_DIR"
fi
mkdir "$LOCK_DIR" 2>/dev/null || die "Another start-arbiter invocation is already in progress"
echo "$$" > "$LOCK_OWNER_FILE"
trap cleanup EXIT INT TERM

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    EXISTING_CMD="$(ps -p "$EXISTING_PID" -o command= 2>/dev/null || true)"
    die "ARBITER already running (PID=$EXISTING_PID, cmd=${EXISTING_CMD:-unknown})"
  fi
  warn "Removing stale PID file: $PID_FILE"
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  LISTEN_PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
  if [[ -n "$LISTEN_PIDS" ]]; then
    DETAILS=()
    while IFS= read -r pid; do
      [[ -z "$pid" ]] && continue
      DETAILS+=("PID=$pid $(ps -p "$pid" -o command= 2>/dev/null || echo unknown)")
    done <<< "$LISTEN_PIDS"
    die "Port $PORT is already in use: ${DETAILS[*]}"
  fi
fi

log "Starting API server on ${HOST}:${PORT}..."
CMD=("$PYTHON_BIN" -m arbiter.main --host "$HOST" --port "$PORT" --log-level "${LOG_LEVEL:-INFO}")
if [[ "${DRY_RUN:-true}" == "false" ]]; then
  CMD+=(--live)
fi
"${CMD[@]}" > /tmp/arbiter-server.log 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

HEALTH_URL="http://127.0.0.1:${PORT}/health"
READY_URL="http://127.0.0.1:${PORT}/ready"
STARTUP_TIMEOUT_S="${ARBITER_STARTUP_TIMEOUT_S:-15}"
DEADLINE=$((SECONDS + STARTUP_TIMEOUT_S))

while (( SECONDS < DEADLINE )); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    tail -n 80 /tmp/arbiter-server.log >&2 || true
    wait "$SERVER_PID" || true
    die "ARBITER exited during startup"
  fi
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  tail -n 80 /tmp/arbiter-server.log >&2 || true
  die "ARBITER did not become healthy at $HEALTH_URL within ${STARTUP_TIMEOUT_S}s"
fi

log "API server started (PID=$SERVER_PID)"
log "ARBITER is running. Logs: /tmp/arbiter-server.log"
log "Local API: http://127.0.0.1:${PORT}"
log "Local dashboard: http://127.0.0.1:${PORT}/ops"
log "Local health: ${HEALTH_URL}"
log "Local ready: ${READY_URL}"
if [[ -n "$LAN_IP" ]]; then
  log "LAN API: http://${LAN_IP}:${PORT}"
  log "LAN dashboard: http://${LAN_IP}:${PORT}/ops"
  log "LAN health: http://${LAN_IP}:${PORT}/health"
  log "LAN ready: http://${LAN_IP}:${PORT}/ready"
else
  warn "Could not detect a LAN IP automatically. Use this machine's IP with port ${PORT} from other devices."
fi

wait "$SERVER_PID"
