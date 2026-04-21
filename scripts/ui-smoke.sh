#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ARBITER_UI_PORT:-8099}"
SERVER_LOG="$(mktemp -t arbiter-ui-smoke.XXXXXX.log)"
SETTINGS_PATH="$(mktemp -t arbiter-ui-settings.XXXXXX.json)"
rm -f "$SETTINGS_PATH"
PWCLI=(npx --yes --package @playwright/cli playwright-cli)
PYTHON_BIN="${ARBITER_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "[ui-smoke] no Python interpreter found. Run ./scripts/setup/bootstrap_python.sh first." >&2
    exit 1
  fi
fi

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$SETTINGS_PATH"
  "${PWCLI[@]}" close >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT_DIR"

echo "[ui-smoke] python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
ARBITER_UI_SMOKE_SEED=1 ARBITER_OPERATOR_SETTINGS_PATH="$SETTINGS_PATH" "$PYTHON_BIN" -m arbiter.main --api-only --port "$PORT" >"$SERVER_LOG" 2>&1 &
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
    const ready = !!document.querySelector('[data-incident-action=\"resolve\"]')
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
LOG_FILTER_STATE="$(${PWCLI[@]} eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  document.querySelector('.log-scope-chip[data-log-scope=\"ops\"]')?.click();
  await sleep(160);
  const cards = Array.from(document.querySelectorAll('.log-category-card[data-log-filter]')).map((card) => ({
    el: card,
    filter: card.getAttribute('data-log-filter'),
    label: card.querySelector('.log-category-name')?.textContent?.trim() || '',
    count: Number.parseInt(card.querySelector('.log-category-count')?.textContent || '0', 10),
  }));
  const selected = cards.find((card) => ['manual', 'incident', 'mapping'].includes(card.filter) && card.count > 0) || null;
  selected?.el?.click();
  await sleep(160);
  const activeScope = document.querySelector('.log-scope-chip.is-active span')?.textContent?.trim();
  const activeCategory = document.querySelector('.log-category-card.is-active .log-category-name')?.textContent?.trim();
  const activeChip = document.querySelector('.log-filter-chip.is-active span')?.textContent?.trim();
  const activeCount = Number.parseInt(document.querySelector('.log-filter-chip.is-active strong')?.textContent || '0', 10);
  const timelineCount = document.querySelectorAll('#logTimeline .log-entry').length;
  const visibleCount = document.getElementById('logVisibleCount')?.textContent?.trim();
  const searchReady = !!document.getElementById('logSearchInput');
  const filterActivated = !!selected && activeCategory === selected.label && activeChip === selected.label;
  const countConsistent = activeCount > 0 && timelineCount === activeCount && visibleCount === (String(activeCount) + ' shown');
  document.querySelector('.log-filter-chip[data-log-filter=\"all\"]')?.click();
  document.querySelector('.log-scope-chip[data-log-scope=\"all\"]')?.click();
  await sleep(160);
  const resetChip = document.querySelector('.log-filter-chip.is-active span')?.textContent?.trim();
  const resetScope = document.querySelector('.log-scope-chip.is-active span')?.textContent?.trim();
  return {
    selectedFilter: selected?.filter || null,
    selectedCategory: selected?.label || null,
    selectedCount: selected?.count || 0,
    activeScope,
    activeCategory,
    activeChip,
    activeCount,
    timelineCount,
    visibleCount,
    searchReady,
    filterActivated,
    countConsistent,
    resetChip,
    resetScope,
  };
})()")"
echo "$LOG_FILTER_STATE"
if ! grep -q '"activeScope": "Ops workflow"' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] ops activity scope did not activate" >&2
  exit 1
fi
if grep -q '"selectedFilter": null' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] no non-empty ops activity category was available to filter" >&2
  exit 1
fi
if ! grep -q '"filterActivated": true' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] ops category filter did not activate" >&2
  exit 1
fi
if ! grep -q '"countConsistent": true' <<<"$LOG_FILTER_STATE"; then
  echo "[ui-smoke] filtered timeline counts drifted from the active chip" >&2
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
MAPPING_ACTION_STATE="$(${PWCLI[@]} eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const statusText = () => document.querySelector('[data-mapping-id="DEM_HOUSE_2026"] [data-mapping-status]')?.textContent?.trim().toLowerCase() || '';
  const tradeText = () => document.querySelector('[data-mapping-id="DEM_HOUSE_2026"] .platform-chip.mapping-trade-pill')?.textContent?.trim().toLowerCase() || '';
  const confirmBtn = document.querySelector('[data-mapping-id="DEM_HOUSE_2026"] [data-mapping-action="confirm"]');
  const confirmBlocked = !!confirmBtn?.disabled;
  const confirmReason = confirmBtn?.title || '';
  document.querySelector('[data-mapping-id="DEM_HOUSE_2026"] [data-mapping-action="enable_auto_trade"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'confirmed' && tradeText().includes('auto-trade')) break;
    await sleep(120);
  }
  const enabledStatus = statusText();
  const enabledTrade = tradeText();
  document.querySelector('[data-mapping-id="DEM_HOUSE_2026"] [data-mapping-action="review"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'review') break;
    await sleep(120);
  }
  return {
    confirmBlocked,
    confirmReason,
    enabledStatus,
    enabledTrade,
    reviewStatus: statusText(),
    reviewTrade: tradeText(),
  };
})()")"
echo "$MAPPING_ACTION_STATE"
if ! grep -q '"confirmBlocked": true' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping confirm guard did not disable an unsafe confirm action" >&2
  exit 1
fi
if ! grep -q 'Confirm blocked - criteria status is' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping confirm guard reason did not surface" >&2
  exit 1
fi
if ! grep -q '"enabledStatus": "confirmed"' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping auto-trade button failed to confirm the route" >&2
  exit 1
fi
if ! grep -q '"enabledTrade": "auto-trade"' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping auto-trade button failed" >&2
  exit 1
fi
if ! grep -q '"reviewStatus": "review"' <<<"$MAPPING_ACTION_STATE"; then
  echo "[ui-smoke] mapping review button failed" >&2
  exit 1
fi

echo "[ui-smoke] exercising incident resolution and search"
INCIDENT_ACTION_STATE="$(${PWCLI[@]} eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const statusText = () => document.querySelector('[data-incident-id] [data-incident-status]')?.textContent?.trim().toLowerCase() || '';
  document.querySelector('[data-incident-id] [data-incident-action="resolve"]')?.click();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (statusText() === 'resolved') break;
    await sleep(120);
  }
  document.querySelector('.log-scope-chip[data-log-scope="ops"]')?.click();
  await sleep(160);
  document.querySelector('.log-filter-chip[data-log-filter="incident"]')?.click();
  await sleep(160);
  const timelineText = Array.from(document.querySelectorAll('#logTimeline .log-entry'))
    .map((entry) => entry.textContent?.trim().toLowerCase() || '')
    .join(' | ');
  const timelineCount = document.querySelectorAll('#logTimeline .log-entry').length;
  const visibleCount = document.getElementById('logVisibleCount')?.textContent?.trim();
  const searchInput = document.getElementById('logSearchInput');
  searchInput.value = 'resolved';
  searchInput.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(160);
  const searchCount = document.querySelectorAll('#logTimeline .log-entry').length;
  const searchVisibleCount = document.getElementById('logVisibleCount')?.textContent?.trim();
  const searchSummary = document.getElementById('logResultSummary')?.textContent?.trim().toLowerCase() || '';
  searchInput.value = '';
  searchInput.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(160);
  return {
    incidentStatus: statusText(),
    timelineCount,
    visibleCount,
    searchCount,
    searchVisibleCount,
    searchSummaryIncludesResolved: searchSummary.includes('resolved'),
    searchReturnedResults: searchCount > 0 && searchCount <= timelineCount,
    countConsistent: timelineCount > 0 && visibleCount === (String(timelineCount) + ' shown'),
    sawResolvedIncident: timelineText.includes('resolved'),
  };
})()")"
echo "$INCIDENT_ACTION_STATE"
if ! grep -q '"incidentStatus": "resolved"' <<<"$INCIDENT_ACTION_STATE"; then
  echo "[ui-smoke] incident resolve button failed" >&2
  exit 1
fi
if ! grep -q '"countConsistent": true' <<<"$INCIDENT_ACTION_STATE"; then
  echo "[ui-smoke] incident log counts drifted after resolution" >&2
  exit 1
fi
if ! grep -q '"searchSummaryIncludesResolved": true' <<<"$INCIDENT_ACTION_STATE"; then
  echo "[ui-smoke] activity search did not summarize the resolved incident query" >&2
  exit 1
fi
if ! grep -q '"searchReturnedResults": true' <<<"$INCIDENT_ACTION_STATE"; then
  echo "[ui-smoke] activity search did not return the resolved incident" >&2
  exit 1
fi
if ! grep -q '"sawResolvedIncident": true' <<<"$INCIDENT_ACTION_STATE"; then
  echo "[ui-smoke] resolved incident did not surface in the log atlas" >&2
  exit 1
fi

echo "[ui-smoke] exercising operator settings"
SETTINGS_STATE="$(${PWCLI[@]} eval "(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const autoToggle = document.querySelector('[data-settings-path=\"auto_executor.enabled\"]');
  const edgeInput = document.querySelector('[data-settings-path=\"scanner.min_edge_cents\"]');
  const cooldownInput = document.querySelector('[data-settings-path=\"alerts.cooldown\"]');
  if (!autoToggle || !edgeInput || !cooldownInput) {
    return { found: false };
  }
  autoToggle.checked = true;
  autoToggle.dispatchEvent(new Event('change', { bubbles: true }));
  edgeInput.value = '4.2';
  edgeInput.dispatchEvent(new Event('input', { bubbles: true }));
  cooldownInput.value = '900';
  cooldownInput.dispatchEvent(new Event('input', { bubbles: true }));
  const draftBadge = document.getElementById('settingsDirtyBadge')?.textContent?.trim() || '';
  document.getElementById('settingsForm')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if ((document.getElementById('settingsDirtyBadge')?.textContent?.trim() || '') === 'Synced') break;
    await sleep(120);
  }
  const savedBadge = document.getElementById('settingsDirtyBadge')?.textContent?.trim() || '';
  const savedMessage = document.getElementById('settingsMessage')?.textContent?.trim() || '';
  const summaryText = Array.from(document.querySelectorAll('.settings-summary-card')).map((card) => card.textContent?.trim() || '').join(' | ');
  location.reload();
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const refreshed = document.querySelector('[data-settings-path=\"scanner.min_edge_cents\"]');
    if (refreshed) break;
    await sleep(120);
  }
  return {
    found: true,
    draftBadge,
    savedBadge,
    savedMessage,
    summaryHasEnabled: summaryText.toLowerCase().includes('enabled'),
    summaryHasEdge: summaryText.includes('4.2c'),
    persistedEdge: document.querySelector('[data-settings-path=\"scanner.min_edge_cents\"]')?.value || '',
    persistedCooldown: document.querySelector('[data-settings-path=\"alerts.cooldown\"]')?.value || '',
    persistedAuto: !!document.querySelector('[data-settings-path=\"auto_executor.enabled\"]')?.checked,
  };
})()")"
echo "$SETTINGS_STATE"
if ! grep -q '"found": true' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] settings surface did not render" >&2
  exit 1
fi
if ! grep -q '"draftBadge": "Draft"' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] settings edits did not produce a draft state" >&2
  exit 1
fi
if ! grep -q '"savedBadge": "Synced"' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] settings save did not return to synced state" >&2
  exit 1
fi
if ! grep -q '"summaryHasEnabled": true' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] settings summary did not reflect the saved auto-trade state" >&2
  exit 1
fi
if ! grep -q '"summaryHasEdge": true' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] settings summary did not reflect the saved scanner edge" >&2
  exit 1
fi
if ! grep -q '"persistedEdge": "4.2"' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] saved scanner edge did not persist across reload" >&2
  exit 1
fi
if ! grep -q '"persistedCooldown": "900"' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] saved alert cooldown did not persist across reload" >&2
  exit 1
fi
if ! grep -q '"persistedAuto": true' <<<"$SETTINGS_STATE"; then
  echo "[ui-smoke] saved auto-trade toggle did not persist across reload" >&2
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
