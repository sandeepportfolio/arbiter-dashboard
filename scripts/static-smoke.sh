#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${ARBITER_STATIC_API_PORT:-8091}"
STATIC_PORT="${ARBITER_STATIC_PORT:-8092}"
API_LOG="$(mktemp -t arbiter-static-api.XXXXXX.log)"
STATIC_LOG="$(mktemp -t arbiter-static-host.XXXXXX.log)"
SETTINGS_PATH="$(mktemp -t arbiter-static-settings.XXXXXX.json)"
OPS_EMAIL_VALUE="${OPS_EMAIL:-${UI_USER_EMAIL:-sparx.sandeep@gmail.com}}"
OPS_PASSWORD_VALUE="${OPS_PASSWORD:-${UI_USER_PASSWORD:-saibaba}}"
rm -f "$SETTINGS_PATH"
PWCLI=(npx --yes --package @playwright/cli playwright-cli)
PYTHON_BIN="${ARBITER_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "[static-smoke] no Python interpreter found. Run ./scripts/setup/bootstrap_python.sh first." >&2
    exit 1
  fi
fi

cleanup() {
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" >/dev/null 2>&1; then
    kill "$API_PID" >/dev/null 2>&1 || true
    wait "$API_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${STATIC_PID:-}" ]] && kill -0 "$STATIC_PID" >/dev/null 2>&1; then
    kill "$STATIC_PID" >/dev/null 2>&1 || true
    wait "$STATIC_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$SETTINGS_PATH"
  "${PWCLI[@]}" close >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT_DIR"

echo "[static-smoke] python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
ARBITER_UI_SMOKE_SEED=1 ARBITER_OPERATOR_SETTINGS_PATH="$SETTINGS_PATH" "$PYTHON_BIN" -m arbiter.main --api-only --port "$API_PORT" >"$API_LOG" 2>&1 &
API_PID=$!
"$PYTHON_BIN" -m http.server "$STATIC_PORT" >"$STATIC_LOG" 2>&1 &
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
OPS_STATE="$(${PWCLI[@]} eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (document.getElementById('authForm')) break;
    await sleep(100);
  }
  document.getElementById('authEmail').value = '${OPS_EMAIL_VALUE}';
  document.getElementById('authPassword').value = '${OPS_PASSWORD_VALUE}';
  document.getElementById('authForm').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const ready = !!document.querySelector('[data-mapping-action="enable_auto_trade"]');
    if (ready) break;
    await sleep(120);
  }
  const statusText = () => document.querySelector('[data-mapping-id="GOP_HOUSE_2026"] [data-mapping-status]')?.textContent?.trim().toLowerCase() || '';
  const tradeText = () => document.querySelector('[data-mapping-id="GOP_HOUSE_2026"] .platform-chip.mapping-trade-pill')?.textContent?.trim().toLowerCase() || '';
  const before = { status: statusText(), trade: tradeText() };
  document.querySelector('[data-mapping-id="GOP_HOUSE_2026"] [data-mapping-action="enable_auto_trade"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'confirmed' && tradeText().includes('auto-trade')) {
      return {
        before,
        after: { status: statusText(), trade: tradeText() },
        route: new URL(window.location.href).searchParams.get('route'),
        hero: document.getElementById('heroTitle')?.textContent?.trim(),
        access: document.getElementById('accessPill')?.textContent?.trim(),
      };
    }
    await sleep(120);
  }
  return {
    before,
    after: { status: statusText(), trade: tradeText() },
    route: new URL(window.location.href).searchParams.get('route'),
    hero: document.getElementById('heroTitle')?.textContent?.trim(),
    access: document.getElementById('accessPill')?.textContent?.trim(),
  };
})()")"
echo "$OPS_STATE"
if ! grep -q '"status": "confirmed"' <<<"$OPS_STATE"; then
  echo "[static-smoke] cross-origin ops status update failed" >&2
  exit 1
fi
if ! grep -q '"trade": "auto-trade"' <<<"$OPS_STATE"; then
  echo "[static-smoke] cross-origin ops trade toggle failed" >&2
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

echo "[static-smoke] saving settings through static shell"
SETTINGS_STATE="$(${PWCLI[@]} eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const edgeInput = document.querySelector('[data-settings-path=\"scanner.min_edge_cents\"]');
  const cooldownInput = document.querySelector('[data-settings-path=\"alerts.cooldown\"]');
  if (!edgeInput || !cooldownInput) {
    return { found: false };
  }
  edgeInput.value = '6.5';
  edgeInput.dispatchEvent(new Event('input', { bubbles: true }));
  cooldownInput.value = '1200';
  cooldownInput.dispatchEvent(new Event('input', { bubbles: true }));
  const draftBadge = document.getElementById('settingsDirtyBadge')?.textContent?.trim() || '';
  document.getElementById('settingsForm')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if ((document.getElementById('settingsDirtyBadge')?.textContent?.trim() || '') === 'Synced') break;
    await sleep(120);
  }
  location.reload();
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const refreshed = document.querySelector('[data-settings-path=\"scanner.min_edge_cents\"]');
    if (refreshed) break;
    await sleep(120);
  }
  return {
    found: true,
    draftBadge,
    savedBadge: document.getElementById('settingsDirtyBadge')?.textContent?.trim() || '',
    persistedEdge: document.querySelector('[data-settings-path=\"scanner.min_edge_cents\"]')?.value || '',
    persistedCooldown: document.querySelector('[data-settings-path=\"alerts.cooldown\"]')?.value || '',
    route: new URL(window.location.href).searchParams.get('route'),
  };
})()")"
echo "$SETTINGS_STATE"
if ! grep -q '"found": true' <<<"$SETTINGS_STATE"; then
  echo "[static-smoke] settings surface did not render in static mode" >&2
  exit 1
fi
if ! grep -q '"draftBadge": "Draft"' <<<"$SETTINGS_STATE"; then
  echo "[static-smoke] static settings edits did not produce a draft state" >&2
  exit 1
fi
if ! grep -q '"savedBadge": "Synced"' <<<"$SETTINGS_STATE"; then
  echo "[static-smoke] static settings save did not return to synced state" >&2
  exit 1
fi
if ! grep -q '"persistedEdge": "6.5"' <<<"$SETTINGS_STATE"; then
  echo "[static-smoke] static settings edge did not persist" >&2
  exit 1
fi
if ! grep -q '"persistedCooldown": "1200"' <<<"$SETTINGS_STATE"; then
  echo "[static-smoke] static settings cooldown did not persist" >&2
  exit 1
fi

echo "[static-smoke] static cross-origin smoke passed"
