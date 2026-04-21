#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${ARBITER_HOST:-0.0.0.0}"
PORT="${ARBITER_PORT:-8100}"
PYTHON_BIN="${ARBITER_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "[run-dashboard-demo] no Python interpreter found. Run ./scripts/setup/bootstrap_python.sh first." >&2
    exit 1
  fi
fi

UI_SESSION_SECRET="${UI_SESSION_SECRET:-$($PYTHON_BIN - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)}"

cd "$ROOT_DIR"
exec env UI_SESSION_SECRET="$UI_SESSION_SECRET" ARBITER_UI_SMOKE_SEED=1 "$PYTHON_BIN" -m arbiter.main --api-only --host "$HOST" --port "$PORT"
