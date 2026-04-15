#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${ARBITER_HOST:-0.0.0.0}"
PORT="${ARBITER_PORT:-8100}"

cd "$ROOT_DIR"
exec env ARBITER_UI_SMOKE_SEED=1 python3 -m arbiter.main --api-only --host "$HOST" --port "$PORT"
