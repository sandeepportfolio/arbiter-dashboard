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
SITE_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (document.querySelectorAll('.metric-card').length > 0) break;
    await sleep(100);
  }
  return {
    title: document.title,
    hero: document.getElementById('heroTitle')?.textContent?.trim(),
    access: document.getElementById('accessPill')?.textContent?.trim(),
    metrics: document.querySelectorAll('.metric-card').length,
    menuItems: document.querySelectorAll('#deskMenu .desk-menu-link').length,
    routesLink: Array.from(document.querySelectorAll('#deskMenu .desk-menu-link'))
      .find((link) => link.getAttribute('href') === '#opportunitiesSection')
      ?.querySelector('.desk-menu-link-label')
      ?.textContent?.trim(),
    opportunities: document.getElementById('opportunityList')?.children.length || 0,
    collectors: document.getElementById('collectorList')?.children.length || 0,
    authHidden: document.getElementById('authOverlay')?.classList.contains('hidden') || false,
    edgeChartReady: !!document.querySelector('#edgeChart svg, #edgeChart .stack-item'),
  };
})()")"
echo "$SITE_STATE"

if ! grep -q '"hero": "Live trading desk"' <<<"$SITE_STATE"; then
  echo "[ui-smoke] public hero title missing" >&2
  exit 1
fi
if ! grep -q '"access": "Read only"' <<<"$SITE_STATE"; then
  echo "[ui-smoke] public access pill is wrong" >&2
  exit 1
fi
if ! grep -q '"metrics": ' <<<"$SITE_STATE" || grep -q '"metrics": 0' <<<"$SITE_STATE"; then
  echo "[ui-smoke] public metric cards did not render" >&2
  exit 1
fi
if ! grep -q '"menuItems": 7' <<<"$SITE_STATE"; then
  echo "[ui-smoke] desk menu did not render the expected section links" >&2
  exit 1
fi
if ! grep -q '"routesLink": "Routes"' <<<"$SITE_STATE"; then
  echo "[ui-smoke] desk menu routes link is missing or mislabelled" >&2
  exit 1
fi
if ! grep -q '"authHidden": true' <<<"$SITE_STATE"; then
  echo "[ui-smoke] auth overlay should be hidden on the public desk" >&2
  exit 1
fi

echo "[ui-smoke] checking public site mobile layout"
"${PWCLI[@]}" resize 390 844 >/dev/null
SITE_MOBILE_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  return {
    width: window.innerWidth,
    hero: document.getElementById('heroTitle')?.textContent?.trim(),
    metrics: document.querySelectorAll('.metric-card').length,
    opportunities: document.getElementById('opportunityList')?.children.length || 0,
  };
})()")"
echo "$SITE_MOBILE_STATE"
if ! grep -q '"width": 390' <<<"$SITE_MOBILE_STATE"; then
  echo "[ui-smoke] public site mobile viewport did not apply" >&2
  exit 1
fi

echo "[ui-smoke] opening operator dashboard"
"${PWCLI[@]}" open "http://127.0.0.1:${PORT}/ops" >/dev/null
DESKTOP_STATE="$("${PWCLI[@]}" eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (document.getElementById('authForm')) break;
    await sleep(120);
  }
  document.getElementById('authEmail').value = 'sparx.sandeep@gmail.com';
  document.getElementById('authPassword').value = 'saibaba';
  document.getElementById('authForm').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const ready = document.querySelectorAll('[data-manual-action]').length >= 2
      && !!document.querySelector('[data-incident-action=\"resolve\"]')
      && !!document.querySelector('[data-mapping-action=\"confirm\"]')
      && document.querySelectorAll('.metric-card').length > 0;
    if (ready) break;
    await sleep(120);
  }
  return {
    title: document.title,
    hero: document.getElementById('heroTitle')?.textContent?.trim(),
    access: document.getElementById('accessPill')?.textContent?.trim(),
    metrics: document.querySelectorAll('.metric-card').length,
    hasOpportunityList: !!document.getElementById('opportunityList'),
    hasCollectorList: !!document.getElementById('collectorList'),
    hasManualQueue: !!document.getElementById('manualQueue'),
    hasIncidentList: !!document.getElementById('incidentList'),
    hasEdgeChart: !!document.querySelector('#edgeChart svg, #edgeChart .stack-item'),
    authHidden: document.getElementById('authOverlay')?.classList.contains('hidden') || false,
  };
})()")"
echo "$DESKTOP_STATE"

if ! grep -q '"hero": "Operator trading desk"' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] dashboard title check failed" >&2
  exit 1
fi
if ! grep -q '"authHidden": true' <<<"$DESKTOP_STATE"; then
  echo "[ui-smoke] operator auth overlay did not close after sign-in" >&2
  exit 1
fi
if ! grep -q '"metrics": ' <<<"$DESKTOP_STATE" || grep -q '"metrics": 0' <<<"$DESKTOP_STATE"; then
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
  document.querySelector('.log-scope-chip[data-log-scope=\"ops\"]')?.click();
  await sleep(160);
  document.querySelector('.log-category-card[data-log-filter=\"manual\"]')?.click();
  await sleep(160);
  const activeScope = document.querySelector('.log-scope-chip.is-active span')?.textContent?.trim();
  const activeCategory = document.querySelector('.log-category-card.is-active .log-category-name')?.textContent?.trim();
  const activeChip = document.querySelector('.log-filter-chip.is-active span')?.textContent?.trim();
  const activeCount = Number.parseInt(document.querySelector('.log-filter-chip.is-active strong')?.textContent || '0', 10);
  const timelineCount = document.querySelectorAll('#logTimeline .log-entry').length;
  const visibleCount = document.getElementById('logVisibleCount')?.textContent?.trim();
  const searchReady = !!document.getElementById('logSearchInput');
  document.querySelector('.log-filter-chip[data-log-filter=\"all\"]')?.click();
  document.querySelector('.log-scope-chip[data-log-scope=\"all\"]')?.click();
  await sleep(160);
  const resetChip = document.querySelector('.log-filter-chip.is-active span')?.textContent?.trim();
  const resetScope = document.querySelector('.log-scope-chip.is-active span')?.textContent?.trim();
  return { activeScope, activeCategory, activeChip, activeCount, timelineCount, visibleCount, searchReady, resetChip, resetScope };
})()")"
echo "$LOG_FILTER_STATE"
if ! grep -q '"activeScope": "Ops workflow"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] ops activity scope did not activate" >&2
  exit 1
fi
if ! grep -q '"activeCategory": "Manual Flow"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] manual category filter did not activate" >&2
  exit 1
fi
if ! grep -q '"activeCount": 4' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] manual filter chip count drifted from the seeded dataset" >&2
  exit 1
fi
if ! grep -q '"timelineCount": 4' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] manual timeline count is not consistent with the active filter" >&2
  exit 1
fi
if ! grep -q '"visibleCount": "4 shown"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] visible count label is not consistent with the manual log feed" >&2
  exit 1
fi
if ! grep -q '"searchReady": true' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] log search input did not render" >&2
  exit 1
fi
if ! grep -q '"resetChip": "All activity"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] all activity filter did not reactivate" >&2
  exit 1
fi
if ! grep -q '"resetScope": "All activity"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] all activity scope did not reactivate" >&2
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
  document.querySelector('.log-filter-chip[data-log-filter=\"manual\"]')?.click();
  await sleep(160);
  const manualLogText = Array.from(document.querySelectorAll('#logTimeline .log-entry'))
    .map((entry) => entry.textContent?.trim().toLowerCase() || '')
    .join(' | ');
  const timelineCount = document.querySelectorAll('#logTimeline .log-entry').length;
  const visibleCount = document.getElementById('logVisibleCount')?.textContent?.trim();
  const searchInput = document.getElementById('logSearchInput');
  searchInput.value = 'cancelled';
  searchInput.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(160);
  const searchCount = document.querySelectorAll('#logTimeline .log-entry').length;
  const searchVisibleCount = document.getElementById('logVisibleCount')?.textContent?.trim();
  const searchSummary = document.getElementById('logResultSummary')?.textContent?.trim().toLowerCase() || '';
  searchInput.value = '';
  searchInput.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(160);
  return {
    enteredStatus,
    closedStatus,
    cancelledStatus: statusText('GOP_SENATE_2026'),
    timelineCount,
    visibleCount,
    searchCount,
    searchVisibleCount,
    searchSummaryIncludesCancelled: searchSummary.includes('cancelled'),
    searchReducedResults: searchCount > 0 && searchCount < timelineCount,
    sawClosedWorkflow: manualLogText.includes('manual closed workflow'),
    sawCancelledWorkflow: manualLogText.includes('manual cancelled workflow'),
  };
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
if ! grep -q '"timelineCount": 4' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual log timeline count drifted after workflow actions" >&2
  exit 1
fi
if ! grep -q '"visibleCount": "4 shown"' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual log visible count drifted after workflow actions" >&2
  exit 1
fi
if ! grep -q '"searchSummaryIncludesCancelled": true' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] activity search did not summarize the cancelled workflow query" >&2
  exit 1
fi
if ! grep -q '"searchReducedResults": true' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] activity search did not narrow the manual workflow log results" >&2
  exit 1
fi
if ! grep -q '"sawClosedWorkflow": true' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual closed workflow did not surface in the log atlas" >&2
  exit 1
fi
if ! grep -q '"sawCancelledWorkflow": true' <<<"$MANUAL_ACTION_STATE"; then
  echo "[ui-smoke] manual cancelled workflow did not surface in the log atlas" >&2
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
