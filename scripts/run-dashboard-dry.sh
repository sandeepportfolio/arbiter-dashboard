#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${ARBITER_HOST:-0.0.0.0}"
PORT="${ARBITER_PORT:-8100}"
UI_SESSION_SECRET="${UI_SESSION_SECRET:-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)}"

cd "$ROOT_DIR"
exec env UI_SESSION_SECRET="$UI_SESSION_SECRET" python3 -m arbiter.main --host "$HOST" --port "$PORT"
