#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${ARBITER_STATIC_API_PORT:-8091}"
STATIC_PORT="${ARBITER_STATIC_PORT:-8092}"
API_LOG="$(mktemp -t arbiter-static-api.XXXXXX.log)"
STATIC_LOG="$(mktemp -t arbiter-static-host.XXXXXX.log)"
PWCLI=(npx --yes --package @playwright/cli playwright-cli)

cleanup() {
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" >/dev/null 2>&1; then
    kill "$API_PID" >/dev/null 2>&1 || true
    wait "$API_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${STATIC_PID:-}" ]] && kill -0 "$STATIC_PID" >/dev/null 2>&1; then
    kill "$STATIC_PID" >/dev/null 2>&1 || true
    wait "$STATIC_PID" >/dev/null 2>&1 || true
  fi
  "${PWCLI[@]}" close >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT_DIR"

ARBITER_UI_SMOKE_SEED=1 python3 -m arbiter.main --api-only --port "$API_PORT" >"$API_LOG" 2>&1 &
API_PID=$!
python3 -m http.server "$STATIC_PORT" >"$STATIC_LOG" 2>&1 &
STATIC_PID=$!

for _ in {1..40}; do
  if curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null && curl -sf "http://127.0.0.1:${STATIC_PORT}/" >/dev/null; then
    break
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null; then
  echo "[static-smoke] API server failed to start" >&2
  cat "$API_LOG" >&2
  exit 1
fi
if ! curl -sf "http://127.0.0.1:${STATIC_PORT}/" >/dev/null; then
  echo "[static-smoke] static server failed to start" >&2
  cat "$STATIC_LOG" >&2
  exit 1
fi

PUBLIC_URL="http://127.0.0.1:${STATIC_PORT}/?api=http://127.0.0.1:${API_PORT}"
OPS_URL="http://127.0.0.1:${STATIC_PORT}/?api=http://127.0.0.1:${API_PORT}&route=%2Fops"

echo "[static-smoke] opening public static desk"
"${PWCLI[@]}" open "$PUBLIC_URL" >/dev/null
PUBLIC_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (document.querySelectorAll('.metric-card').length > 0) break;
    await sleep(100);
  }
  return {
    title: document.title,
    hero: document.getElementById('heroTitle')?.textContent?.trim(),
    access: document.getElementById('accessPill')?.textContent?.trim(),
    metrics: document.querySelectorAll('.metric-card').length,
    apiParam: new URL(window.location.href).searchParams.get('api'),
    authHidden: document.getElementById('authOverlay')?.classList.contains('hidden') || false,
  };
})()")"
echo "$PUBLIC_STATE"
if ! grep -q '"hero": "Live trading desk"' <<<"$PUBLIC_STATE"; then
  echo "[static-smoke] public static desk failed to render" >&2
  exit 1
fi
if ! grep -q '"apiParam": "http://127.0.0.1:' <<<"$PUBLIC_STATE"; then
  echo "[static-smoke] static api override was not applied" >&2
  exit 1
fi
if ! grep -q '"authHidden": true' <<<"$PUBLIC_STATE"; then
  echo "[static-smoke] public static desk should stay read-only" >&2
  exit 1
fi

echo "[static-smoke] opening operator route via static shell"
"${PWCLI[@]}" open "$OPS_URL" >/dev/null
OPS_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (document.getElementById('authForm')) break;
    await sleep(100);
  }
  document.getElementById('authEmail').value = 'sparx.sandeep@gmail.com';
  document.getElementById('authPassword').value = 'saibaba';
  document.getElementById('authForm').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const ready = document.querySelectorAll('[data-manual-action]').length >= 2
      && !!document.querySelector('[data-mapping-action=\"confirm\"]');
    if (ready) break;
    await sleep(120);
  }
  const before = document.querySelector('[data-manual-canonical=\"DEM_SENATE_2026\"] [data-manual-status]')?.textContent?.trim().toLowerCase() || '';
  document.querySelector('[data-manual-canonical=\"DEM_SENATE_2026\"] [data-manual-action=\"mark_entered\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const after = document.querySelector('[data-manual-canonical=\"DEM_SENATE_2026\"] [data-manual-status]')?.textContent?.trim().toLowerCase() || '';
    if (after === 'entered') {
      return {
        before,
        after,
        route: new URL(window.location.href).searchParams.get('route'),
        hero: document.getElementById('heroTitle')?.textContent?.trim(),
        access: document.getElementById('accessPill')?.textContent?.trim(),
      };
    }
    await sleep(120);
  }
  return {
    before,
    after: document.querySelector('[data-manual-canonical=\"DEM_SENATE_2026\"] [data-manual-status]')?.textContent?.trim().toLowerCase() || '',
    route: new URL(window.location.href).searchParams.get('route'),
    hero: document.getElementById('heroTitle')?.textContent?.trim(),
    access: document.getElementById('accessPill')?.textContent?.trim(),
  };
})()")"
echo "$OPS_STATE"
if ! grep -q '"after": "entered"' <<<"$OPS_STATE"; then
  echo "[static-smoke] cross-origin ops action failed" >&2
  exit 1
fi
if ! grep -q '"route": "/ops"' <<<"$OPS_STATE"; then
  echo "[static-smoke] ops route was not applied" >&2
  exit 1
fi
if ! grep -q '"hero": "Operator trading desk"' <<<"$OPS_STATE"; then
  echo "[static-smoke] operator desk did not load in static mode" >&2
  exit 1
fi

echo "[static-smoke] static cross-origin smoke passed"
