#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ARBITER_UI_PORT:-8099}"
SERVER_LOG="$(mktemp -t arbiter-ui-smoke.XXXXXX.log)"
PWCLI=(npx --yes --package @playwright/cli playwright-cli)

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  "${PWCLI[@]}" close >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT_DIR"

ARBITER_UI_SMOKE_SEED=1 python3 -m arbiter.main --api-only --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in {1..40}; do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null; then
    break
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null; then
  echo "[ui-smoke] server failed to start" >&2
  cat "$SERVER_LOG" >&2
  exit 1
fi

echo "[ui-smoke] opening desktop dashboard"
"${PWCLI[@]}" open "http://127.0.0.1:${PORT}" >/dev/null
DESKTOP_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const ready = document.querySelectorAll('.metric-card').length === 5
      && document.querySelectorAll('[data-manual-action]').length >= 2
      && !!document.querySelector('[data-incident-action=\"resolve\"]')
      && !!document.querySelector('[data-mapping-action=\"confirm\"]')
      && !!document.querySelector('#edgeChart svg');
    if (ready) {
      return {
        title: document.title,
        metrics: document.querySelectorAll('.metric-card').length,
        panels: document.querySelectorAll('.panel').length,
        hasOpportunityList: !!document.getElementById('opportunityList'),
        hasCollectorList: !!document.getElementById('collectorList'),
        hasManualQueue: !!document.getElementById('manualQueue'),
        hasIncidentList: !!document.getElementById('incidentList'),
        hasEdgeChart: !!document.querySelector('#edgeChart svg, #edgeChart .stack-item'),
      };
    }
    await sleep(120);
  }
  return { timeout: true, title: document.title, metrics: document.querySelectorAll('.metric-card').length };
})()")"
echo "$DESKTOP_STATE"

if ! grep -q '"title": "ARBITER"' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] dashboard title check failed" >&2
  exit 1
fi
if ! grep -q '"metrics": 5' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] metric card count check failed" >&2
  exit 1
fi
if ! grep -q '"hasOpportunityList": true' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] opportunity list missing" >&2
  exit 1
fi
if ! grep -q '"hasCollectorList": true' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] collector list missing" >&2
  exit 1
fi
if ! grep -q '"hasManualQueue": true' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] manual queue missing" >&2
  exit 1
fi
if ! grep -q '"hasIncidentList": true' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] incident queue missing" >&2
  exit 1
fi

echo "[ui-smoke] checking log filters"
LOG_FILTER_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  document.querySelector('.log-category-card[data-log-filter=\"manual\"]')?.click();
  await sleep(160);
  const activeCategory = document.querySelector('.log-category-card.is-active .log-category-name')?.textContent?.trim();
  const activeChip = document.querySelector('.log-filter-chip.is-active span')?.textContent?.trim();
  const timelineCount = document.querySelectorAll('#logTimeline .log-entry').length;
  document.querySelector('.log-filter-chip[data-log-filter=\"all\"]')?.click();
  await sleep(160);
  const resetChip = document.querySelector('.log-filter-chip.is-active span')?.textContent?.trim();
  return { activeCategory, activeChip, timelineCount, resetChip };
})()")"
echo "$LOG_FILTER_STATE"
if ! grep -q '"activeCategory": "Manual Flow"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] manual category filter did not activate" >&2
  exit 1
fi
if ! grep -q '"resetChip": "All activity"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] all activity filter did not reactivate" >&2
  exit 1
fi

echo "[ui-smoke] exercising mapping buttons"
MAPPING_ACTION_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const statusText = () => document.querySelector('[data-mapping-id=\"DEM_HOUSE_2026\"] [data-mapping-status]')?.textContent?.trim().toLowerCase() || '';
  const autoTradeText = () => document.querySelector('[data-mapping-id=\"DEM_HOUSE_2026\"] .operator-meta-row span')?.textContent?.trim().toLowerCase() || '';
  document.querySelector('[data-mapping-id=\"DEM_HOUSE_2026\"] [data-mapping-action=\"confirm\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'confirmed') break;
    await sleep(120);
  }
  const confirmStatus = statusText();
  document.querySelector('[data-mapping-id=\"DEM_HOUSE_2026\"] [data-mapping-action=\"enable_auto_trade\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (autoTradeText().includes('auto-trade allowed')) break;
    await sleep(120);
  }
  const enabledAutoTrade = autoTradeText();
  document.querySelector('[data-mapping-id=\"DEM_HOUSE_2026\"] [data-mapping-action=\"review\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'review') break;
    await sleep(120);
  }
  return { confirmStatus, enabledAutoTrade, reviewStatus: statusText() };
})()")"
echo "$MAPPING_ACTION_STATE"
if ! grep -q '"confirmStatus": "confirmed"' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping confirm button failed" >&2
  exit 1
fi
if ! grep -q '"enabledAutoTrade": "auto-trade allowed"' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping auto-trade button failed" >&2
  exit 1
fi
if ! grep -q '"reviewStatus": "review"' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping review button failed" >&2
  exit 1
fi

echo "[ui-smoke] exercising manual workflow buttons"
MANUAL_ACTION_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const statusText = (canonicalId) => document.querySelector('[data-manual-canonical=\"' + canonicalId + '\"] [data-manual-status]')?.textContent?.trim().toLowerCase() || '';
  document.querySelector('[data-manual-canonical=\"DEM_SENATE_2026\"] [data-manual-action=\"mark_entered\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText('DEM_SENATE_2026') === 'entered') break;
    await sleep(120);
  }
  const enteredStatus = statusText('DEM_SENATE_2026');
  document.querySelector('[data-manual-canonical=\"DEM_SENATE_2026\"] [data-manual-action=\"mark_closed\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText('DEM_SENATE_2026') === 'closed') break;
    await sleep(120);
  }
  const closedStatus = statusText('DEM_SENATE_2026');
  document.querySelector('[data-manual-canonical=\"GOP_SENATE_2026\"] [data-manual-action=\"cancel\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText('GOP_SENATE_2026') === 'cancelled') break;
    await sleep(120);
  }
  return { enteredStatus, closedStatus, cancelledStatus: statusText('GOP_SENATE_2026') };
})()")"
echo "$MANUAL_ACTION_STATE"
if ! grep -q '"enteredStatus": "entered"' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual entered button failed" >&2
  exit 1
fi
if ! grep -q '"closedStatus": "closed"' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual closed button failed" >&2
  exit 1
fi
if ! grep -q '"cancelledStatus": "cancelled"' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual cancel button failed" >&2
  exit 1
fi

echo "[ui-smoke] exercising incident resolution button"
INCIDENT_ACTION_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const statusText = () => document.querySelector('[data-incident-id] [data-incident-status]')?.textContent?.trim().toLowerCase() || '';
  document.querySelector('[data-incident-id] [data-incident-action=\"resolve\"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'resolved') break;
    await sleep(120);
  }
  return { incidentStatus: statusText() };
})()")"
echo "$INCIDENT_ACTION_STATE"
if ! grep -q '"incidentStatus": "resolved"' <<<"$INCIDENT_ACTION_STATE"; then
  echo "[ui-smoke] incident resolve button failed" >&2
  exit 1
fi

CONSOLE_STATE="$("${PWCLI[@]}" console)"
echo "$CONSOLE_STATE"
if ! grep -q 'Errors: 0, Warnings: 0' <<<"$CONSOLE_STATE"; then
  echo "[ui-smoke] console reported errors or warnings" >&2
  exit 1
fi

echo "[ui-smoke] checking mobile layout"
"${PWCLI[@]}" resize 390 844 >/dev/null
MOBILE_STATE="$("${PWCLI[@]}" eval "() => ({ width: window.innerWidth, modeText: document.getElementById('modePill')?.textContent?.trim(), cards: document.querySelectorAll('.metric-card').length, panels: document.querySelectorAll('.panel').length })")"
echo "$MOBILE_STATE"

if ! grep -q '"width": 390' <<<"$MOBILE_STATE"; then
  echo "[ui-smoke] mobile resize check failed" >&2
  exit 1
fi

echo "[ui-smoke] dashboard smoke passed"
