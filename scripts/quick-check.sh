#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ARBITER_SMOKE_PORT:-8092}"
SERVER_LOG="$(mktemp -t arbiter-quick-check.XXXXXX.log)"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$ROOT_DIR"

echo "[quick-check] compile package"
python3 -m compileall arbiter >/dev/null

echo "[quick-check] run python package tests"
python3 -m pytest -q arbiter

if [[ -d node_modules ]]; then
  echo "[quick-check] run TypeScript typecheck"
  npm run typecheck
  echo "[quick-check] run TypeScript tests"
  npm test
else
  echo "[quick-check] skipping Node checks because node_modules is missing (run npm ci once to enable them)"
fi

echo "[quick-check] smoke test API server"
python3 -m arbiter.main --api-only --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in {1..30}; do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null && curl -sf "http://127.0.0.1:${PORT}/" >/dev/null; then
    echo "[quick-check] smoke test passed"
    exit 0
  fi
  sleep 1
done

echo "[quick-check] smoke test failed; server log follows:" >&2
cat "$SERVER_LOG" >&2
exit 1
