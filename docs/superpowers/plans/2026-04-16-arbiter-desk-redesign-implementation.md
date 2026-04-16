# Arbiter Desk Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the remaining UI defects in the Arbiter public and operator dashboards, compact Activity Atlas, and redesign mapping into a bounded high-volume workspace without changing backend behavior.

**Architecture:** Keep the existing fetch/WebSocket/state pipeline in `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`, but move dense UI presentation into stronger view-model helpers and bounded pane layouts. Stabilize shared control primitives first, then apply them to scanner rows, Activity Atlas, and the mapping workbench so the same visual rules hold on desktop, tablet, and mobile.

**Tech Stack:** Static HTML, vanilla JS, shared CSS, Vitest, Playwright-based node scripts in `output/`, existing dashboard fetch/WebSocket pipeline

---

## File Map

- `C:\Users\sande\Documents\arbiter-dashboard\index.html` - static-host entrypoint; must mirror the UI shell used by the served dashboard.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.html` - API-served dashboard markup.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css` - canonical visual system, responsive layout rules, button/chip behavior, atlas and mapping workbench styling.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js` - render pipeline, local UI state, DOM event handling, mapping/atlas rendering.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\activity-atlas-model.js` - atlas filtering and presentation shaping.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\activity-atlas-model.test.js` - atlas unit tests.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\mapping-workspace-model.js` - new pure helper for high-volume mapping views, row windowing, selection, and inspector summaries.
- `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\mapping-workspace-model.test.js` - unit tests for the mapping workbench helper.
- `C:\Users\sande\Documents\arbiter-dashboard\output\verify_dashboard_polish.mjs` - dashboard layout + hover stability audit.
- `C:\Users\sande\Documents\arbiter-dashboard\output\verify_activity_atlas.mjs` - Activity Atlas-specific audit.
- `C:\Users\sande\Documents\arbiter-dashboard\output\ui_verify.mjs` - broad public/ops smoke verification across breakpoints.

---

### Task 1: Lock shared control geometry before redesigning surfaces

**Files:**
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\output\verify_dashboard_polish.mjs`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`

- [ ] **Step 1: Write the failing hover/alignment audit**

Add hover-state geometry checks and operator-button overflow checks to `C:\Users\sande\Documents\arbiter-dashboard\output\verify_dashboard_polish.mjs`.

```js
async function hoverRect(page, selector) {
  const target = page.locator(selector).first();
  const before = await target.boundingBox();
  await target.hover();
  await page.waitForTimeout(120);
  const after = await target.boundingBox();
  return {
    selector,
    before: before && {
      width: Math.round(before.width),
      height: Math.round(before.height),
    },
    after: after && {
      width: Math.round(after.width),
      height: Math.round(after.height),
    },
  };
}

const hoverAudits = await Promise.all([
  hoverRect(page, '.filter-pill'),
  hoverRect(page, '.action-button'),
  hoverRect(page, '.log-filter-chip'),
]);

const operatorCards = await page.evaluate(() => {
  return [...document.querySelectorAll('#manualQueue .operator-card, #incidentList .operator-card, #mappingList .operator-card')].map((card) => ({
    text: card.textContent.replace(/\s+/g, ' ').trim(),
    clientWidth: card.clientWidth,
    scrollWidth: card.scrollWidth,
    buttonOverflow: [...card.querySelectorAll('.action-button')].some((button) => button.scrollWidth > button.clientWidth + 1),
  }));
});
```

- [ ] **Step 2: Run the audit to verify it fails on the current UI**

Run: `node output/verify_dashboard_polish.mjs`

Expected: FAIL with one or more messages about hover geometry drift, operator button overflow, or control truncation.

- [ ] **Step 3: Implement shared fixed-geometry control primitives**

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js` so action buttons always render a stable text wrapper.

```js
function renderActionButton(label, action, scope, targetId, canonicalId, secondary = false) {
  return `
    <button
      type="button"
      class="action-button ${secondary ? 'action-button-secondary' : ''}"
      data-${scope}-action="${escapeHtml(action)}"
      data-target-id="${escapeHtml(targetId)}"
      data-canonical-id="${escapeHtml(canonicalId || '')}"
    >
      <span class="action-button-label">${escapeHtml(label)}</span>
    </button>
  `;
}
```

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css` so pills, chips, badges, and action buttons never change their geometry on hover/focus.

```css
.filter-pill,
.log-filter-chip,
.log-scope-chip,
.action-button,
.action-button-secondary,
.panel-badge,
.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 38px;
  line-height: 1;
  white-space: nowrap;
  vertical-align: middle;
}

.filter-pill,
.action-button,
.action-button-secondary {
  padding: 0 12px;
}

.action-button-label {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
}

.filter-pill:hover,
.filter-pill:focus-visible,
.log-filter-chip:hover,
.log-filter-chip:focus-visible,
.log-scope-chip:hover,
.log-scope-chip:focus-visible,
.action-button:hover,
.action-button:focus-visible,
.action-button-secondary:hover,
.action-button-secondary:focus-visible {
  transform: none;
}
```

- [ ] **Step 4: Run the dashboard audit again to verify the controls are stable**

Run: `node output/verify_dashboard_polish.mjs`

Expected: PASS for hover geometry checks and no action-button overflow in the operator cards touched by the script.

- [ ] **Step 5: Commit**

```bash
git add output/verify_dashboard_polish.mjs arbiter/web/dashboard.js arbiter/web/styles.css
git commit -m "fix: stabilize dashboard control geometry"
```

---

### Task 2: Tighten scanner rows and operator card alignment

**Files:**
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\output\ui_verify.mjs`

- [ ] **Step 1: Write the failing scanner/operator layout checks**

Extend `C:\Users\sande\Documents\arbiter-dashboard\output\ui_verify.mjs` to assert that operator card headers, status badges, and action rows fit without overflow.

```js
const operatorFacts = await page.evaluate(() => {
  return [...document.querySelectorAll('#manualQueue .operator-card, #incidentList .operator-card')].map((card) => {
    const header = card.querySelector('.stack-item-header');
    const title = card.querySelector('.stack-item-title');
    const badge = card.querySelector('[data-manual-status], [data-incident-status]');
    const actions = card.querySelector('.action-row');
    return {
      title: title?.textContent?.trim() || '',
      cardOverflow: card.scrollWidth > card.clientWidth + 1,
      headerOverflow: header ? header.scrollWidth > header.clientWidth + 1 : false,
      badgeOverflow: badge ? badge.scrollWidth > badge.clientWidth + 1 : false,
      actionOverflow: actions ? actions.scrollWidth > actions.clientWidth + 1 : false,
    };
  });
});
```

- [ ] **Step 2: Run the broad UI sweep and verify it fails on the current compact desktop/mobile cases**

Run: `node output/ui_verify.mjs`

Expected: FAIL with at least one operator-card or scanner-row alignment error.

- [ ] **Step 3: Rebuild the scanner side column and operator action layout with stable grid boundaries**

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`.

```css
.blotter-row {
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}

.blotter-row-main {
  gap: 12px;
}

.blotter-row-side {
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  justify-items: stretch;
}

.blotter-chip-row,
.mapping-platforms,
.operator-meta-row,
.action-row {
  align-items: center;
}

.stack-item-header {
  align-items: start;
}

.stack-item-title {
  min-width: 0;
  max-width: 26ch;
}

.operator-meta-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px;
}

.action-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.action-row .action-button,
.action-row .action-button-secondary {
  flex: 0 0 auto;
}
```

Shorten operator button labels in `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js` where needed so they read as controls, not sentences.

```js
buttons.push(renderActionButton('Entered', 'mark_entered', 'manual', position.position_id, position.canonical_id));
buttons.push(renderActionButton('Cancel', 'cancel', 'manual', position.position_id, position.canonical_id, true));
buttons.push(renderActionButton('Closed', 'mark_closed', 'manual', position.position_id, position.canonical_id));
```

- [ ] **Step 4: Run the UI sweep again to verify scanner rows and operator cards are clean**

Run: `node output/ui_verify.mjs`

Expected: PASS with no operator-card overflow and no new desktop/mobile layout errors.

- [ ] **Step 5: Commit**

```bash
git add arbiter/web/styles.css arbiter/web/dashboard.js output/ui_verify.mjs
git commit -m "fix: align scanner and operator dashboard cards"
```

---

### Task 3: Compact Activity Atlas into terse operational events

**Files:**
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\activity-atlas-model.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\activity-atlas-model.test.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\output\verify_activity_atlas.mjs`

- [ ] **Step 1: Write failing unit tests for terse atlas presentation**

Extend `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\activity-atlas-model.test.js` with a pure presentation helper test.

```js
import { buildActivityAtlasView, presentActivityEntry } from './activity-atlas-model.js';

it('compresses activity entries into title line, meta line, and compact tags', () => {
  const presented = presentActivityEntry({
    category: 'opportunity',
    title: 'BTC to 100k before year-end',
    headline: 'Tradable route published',
    narrative: 'Kalshi YES against PredictIt NO with edge 18.4c',
    tags: ['3/3 scans', 'tradable', 'edge 18.4c', 'qty 4000'],
    timestamp: 120,
    tone: 'tone-mint',
    venuePath: 'Kalshi -> PredictIt',
    status: 'tradable',
    metric: 'edge 18.4c',
  }, 180);

  expect(presented.titleLine).toBe('Route published - Ready');
  expect(presented.metaLine).toBe('Kalshi -> PredictIt - 1m ago - edge 18.4c');
  expect(presented.tags).toEqual(['3/3 scans', 'tradable', 'edge 18.4c']);
});
```

- [ ] **Step 2: Run the atlas unit test to verify it fails**

Run: `npx vitest run arbiter/web/activity-atlas-model.test.js`

Expected: FAIL because `presentActivityEntry` does not exist yet.

- [ ] **Step 3: Implement a pure atlas presentation helper**

Add `presentActivityEntry` to `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\activity-atlas-model.js`.

```js
function relTime(timestamp, nowTimestamp = Date.now() / 1000) {
  if (!timestamp) return 'just now';
  const delta = Math.max(0, nowTimestamp - timestamp);
  if (delta < 10) return 'just now';
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  return `${Math.round(delta / 3600)}h ago`;
}

const STATUS_LABELS = {
  tradable: 'Ready',
  confirmed: 'Ready',
  submitted: 'Pending',
  filled: 'Settled',
  failed: 'Failed',
  manual: 'Waiting',
  review: 'Review',
  stale: 'Stale',
  open: 'Open',
  resolved: 'Resolved',
};

export function presentActivityEntry(entry, nowTimestamp = Date.now() / 1000) {
  const status = STATUS_LABELS[String(entry.status || '').toLowerCase()] || 'Active';
  const titleLine = `${entry.eventVerb || entry.title} - ${status}`;
  const metaParts = [entry.venuePath, relTime(entry.timestamp, nowTimestamp), entry.metric].filter(Boolean);
  return {
    ...entry,
    statusLabel: status,
    titleLine,
    metaLine: metaParts.join(' - '),
    tags: (entry.tags || []).slice(0, 3),
  };
}
```

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js` so the import line includes `presentActivityEntry`, the entry builders expose terse UI fields, and `renderLogEntry` uses the presented entry instead of long-form copy.

```js
import { buildActivityAtlasView, presentActivityEntry } from './activity-atlas-model.js';

function buildOpportunityEntry(opp, index) {
  return {
    id: `opportunity-${opp.canonical_id}-${index}`,
    category: 'opportunity',
    tone: opp.status === 'tradable' ? 'tone-mint' : opp.status === 'manual' ? 'tone-plum' : 'tone-slate',
    title: opp.description || opp.canonical_id,
    headline: `${titleCase(opp.status || 'candidate')} route for cross-venue execution`,
    narrative: `${platformLabel(opp.yes_platform)} YES matched with ${platformLabel(opp.no_platform)} NO.`,
    tags: [
      `${formatWhole.format(opp.persistence_count || 0)}/${formatWhole.format(state.system?.scanner?.persistence_scans || 0)} scans`,
      `qty ${formatWhole.format(opp.suggested_qty || 0)}`,
      `edge ${cents(opp.net_edge_cents || 0)}`,
    ],
    footnote: 'Published live',
    timestamp: Number(opp.timestamp || state.system?.timestamp || Date.now() / 1000) - (index * 0.002),
    eventVerb: 'Route published',
    venuePath: `${platformLabel(opp.yes_platform)} -> ${platformLabel(opp.no_platform)}`,
    status: opp.status,
    metric: `edge ${cents(opp.net_edge_cents || 0)}`,
    summary: `${formatWhole.format(opp.persistence_count || 0)}/${formatWhole.format(state.system?.scanner?.persistence_scans || 0)} scans - qty ${formatWhole.format(opp.suggested_qty || 0)}`,
  };
}

function renderLogEntry(entry) {
  const presented = presentActivityEntry(entry, Date.now() / 1000);
  const category = LOG_DEFINITIONS[presented.category];
  return `
    <article class="log-entry ${presented.tone}">
      <div class="log-entry-head">
        <div class="log-entry-kicker">
          <span class="log-entry-source">${escapeHtml(category.label)}</span>
          <span class="log-entry-dot"></span>
          <span>${escapeHtml(relTime(presented.timestamp))}</span>
        </div>
        <span class="log-entry-badge">${escapeHtml(presented.statusLabel || category.label)}</span>
      </div>
      <div class="log-entry-body">
        <div>
          <h3>${escapeHtml(presented.titleLine)}</h3>
          <p class="log-entry-headline">${escapeHtml(presented.metaLine)}</p>
        </div>
      </div>
      <p class="log-entry-narrative">${escapeHtml(presented.summary || presented.narrative)}</p>
      <div class="log-entry-tags">${presented.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('')}</div>
    </article>
  `;
}
```

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css` to keep entries compact.

```css
.log-entry {
  gap: 10px;
  padding: 14px 16px 14px 18px;
}

.log-entry-body h3 {
  font-size: 0.98rem;
  line-height: 1.2;
}

.log-entry-headline,
.log-entry-narrative {
  font-size: 0.82rem;
  line-height: 1.45;
}

.log-entry-tags span {
  padding: 4px 8px;
  font-size: 0.68rem;
}
```

- [ ] **Step 4: Extend the Playwright atlas audit and verify the atlas is compact and bounded**

Add to `C:\Users\sande\Documents\arbiter-dashboard\output\verify_activity_atlas.mjs`:

```js
const entryFacts = await page.evaluate(() => {
  return [...document.querySelectorAll('.log-entry')].slice(0, 6).map((entry) => ({
    text: entry.textContent.replace(/\s+/g, ' ').trim(),
    clientWidth: entry.clientWidth,
    scrollWidth: entry.scrollWidth,
    clientHeight: entry.clientHeight,
    scrollHeight: entry.scrollHeight,
  }));
});
```

Run:
- `npx vitest run arbiter/web/activity-atlas-model.test.js`
- `node output/verify_activity_atlas.mjs`

Expected: PASS with no atlas entry overflow and sticky/header behavior still intact.

- [ ] **Step 5: Commit**

```bash
git add arbiter/web/activity-atlas-model.js arbiter/web/activity-atlas-model.test.js arbiter/web/dashboard.js arbiter/web/styles.css output/verify_activity_atlas.mjs
git commit -m "feat: compact activity atlas presentation"
```

---

### Task 4: Replace the mapping list with a bounded high-volume workbench

**Files:**
- Create: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\mapping-workspace-model.js`
- Create: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\mapping-workspace-model.test.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.html`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\index.html`
- Modify: `C:\Users\sande\Documents\arbiter-dashboard\output\verify_dashboard_polish.mjs`

- [ ] **Step 1: Write the failing mapping workspace unit tests**

Create `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\mapping-workspace-model.test.js`.

```js
import { describe, expect, it } from 'vitest';
import { buildMappingWorkspaceView } from './mapping-workspace-model.js';

const mappings = Array.from({ length: 200 }, (_, index) => ({
  canonical_id: `map-${index}`,
  description: `Election event ${index}`,
  status: index % 5 === 0 ? 'disabled' : index % 3 === 0 ? 'review' : 'confirmed',
  allow_auto_trade: index % 4 === 0,
  updated_at: 1_713_263_600 - index,
  kalshi: `K-${index}`,
  polymarket: `P-${index}`,
  predictit: index % 2 === 0 ? `PI-${index}` : '',
  confidence: 0.92,
}));

describe('mapping workspace model', () => {
  it('builds saved-view counts and returns a bounded visible row window', () => {
    const view = buildMappingWorkspaceView({
      mappings,
      activeView: 'review',
      query: 'Election',
      selectedId: 'map-9',
      rowHeight: 44,
      viewportHeight: 352,
      scrollTop: 132,
    });

    expect(view.views.review.count).toBeGreaterThan(0);
    expect(view.visibleRows.length).toBeLessThanOrEqual(10);
    expect(view.selected?.canonicalId).toBe('map-9');
  });
});
```

- [ ] **Step 2: Run the mapping workspace test to verify it fails**

Run: `npx vitest run arbiter/web/mapping-workspace-model.test.js`

Expected: FAIL because the new model file does not exist yet.

- [ ] **Step 3: Implement the pure mapping workspace helper**

Create `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\mapping-workspace-model.js`.

```js
const VIEW_FILTERS = {
  all: () => true,
  unmapped: (mapping) => !mapping.kalshi || !mapping.polymarket || !mapping.predictit,
  review: (mapping) => String(mapping.status || '').toLowerCase() === 'review',
  conflict: (mapping) => String(mapping.status || '').toLowerCase() === 'disabled',
  ready: (mapping) => String(mapping.status || '').toLowerCase() === 'confirmed',
  held: (mapping) => !mapping.allow_auto_trade,
};

export function buildMappingWorkspaceView({ mappings = [], activeView = 'all', query = '', selectedId = '', rowHeight = 44, viewportHeight = 352, scrollTop = 0 }) {
  const normalizedQuery = String(query || '').trim().toLowerCase();
  const filtered = mappings.filter((mapping) => {
    const viewFilter = VIEW_FILTERS[activeView] || VIEW_FILTERS.all;
    const haystack = [mapping.description, mapping.canonical_id, mapping.kalshi, mapping.polymarket, mapping.predictit].filter(Boolean).join(' ').toLowerCase();
    return viewFilter(mapping) && (!normalizedQuery || haystack.includes(normalizedQuery));
  });
  const start = Math.max(0, Math.floor(scrollTop / rowHeight));
  const visibleCount = Math.max(1, Math.ceil(viewportHeight / rowHeight) + 2);
  const visibleRows = filtered.slice(start, start + visibleCount).map((mapping) => ({
    id: mapping.canonical_id,
    canonicalId: mapping.canonical_id,
    title: mapping.description || mapping.canonical_id,
    status: String(mapping.status || 'review'),
    freshness: mapping.updated_at,
    venues: [mapping.kalshi, mapping.polymarket, mapping.predictit].filter(Boolean),
    allowAutoTrade: !!mapping.allow_auto_trade,
  }));
  const selected = filtered.find((mapping) => mapping.canonical_id === selectedId) || filtered[0] || null;
  const views = Object.fromEntries(Object.keys(VIEW_FILTERS).map((key) => [key, { key, count: mappings.filter(VIEW_FILTERS[key]).length }]));
  return { views, rows: filtered, visibleRows, selected, start, rowHeight, totalHeight: filtered.length * rowHeight };
}
```

- [ ] **Step 4: Replace the mapping markup in both HTML entrypoints with a three-pane workspace shell**

Update both `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.html` and `C:\Users\sande\Documents\arbiter-dashboard\index.html`.

```html
<article class="panel mapping-workspace-panel">
  <div class="panel-header">
    <div>
      <p class="panel-kicker">Mapping</p>
      <h2>Canonical market map</h2>
    </div>
    <span id="mappingCount" class="panel-badge">0</span>
  </div>
  <div class="mapping-workspace">
    <aside id="mappingViews" class="mapping-sidebar"></aside>
    <section class="mapping-console">
      <div class="mapping-toolbar">
        <label class="mapping-search">
          <span class="sr-only">Search mappings</span>
          <input id="mappingSearch" type="search" placeholder="Search canonical events or venue contracts">
        </label>
        <div id="mappingQuickFilters" class="mapping-filter-row"></div>
      </div>
      <div class="mapping-table-shell">
        <div class="mapping-table-head">
          <span>Event</span>
          <span>Venues</span>
          <span>Status</span>
          <span>Updated</span>
        </div>
        <div id="mappingTableViewport" class="mapping-table-viewport"></div>
      </div>
    </section>
    <aside id="mappingInspector" class="mapping-inspector"></aside>
  </div>
</article>
```

- [ ] **Step 5: Wire the new mapping workspace renderer into the dashboard**

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`.

```js
import { buildMappingWorkspaceView } from './mapping-workspace-model.js';

state.mappingView = 'all';
state.mappingQuery = '';
state.mappingSelectedId = '';
state.mappingScrollTop = 0;
const mappingSearchInputEl = document.getElementById('mappingSearch');
const mappingViewportEl = document.getElementById('mappingTableViewport');

function renderMappings() {
  const viewsEl = document.getElementById('mappingViews');
  const viewportEl = document.getElementById('mappingTableViewport');
  const inspectorEl = document.getElementById('mappingInspector');
  const countEl = document.getElementById('mappingCount');
  const view = buildMappingWorkspaceView({
    mappings: state.mappings,
    activeView: state.mappingView,
    query: state.mappingQuery,
    selectedId: state.mappingSelectedId,
    rowHeight: 44,
    viewportHeight: 352,
    scrollTop: state.mappingScrollTop,
  });

  if (countEl) countEl.textContent = formatWhole.format(view.rows.length);
  if (viewsEl) {
    viewsEl.innerHTML = Object.values(view.views).map((item) => `
      <button type="button" class="mapping-view-chip ${state.mappingView === item.key ? 'is-active' : ''}" data-mapping-view="${escapeHtml(item.key)}">
        <span>${escapeHtml(titleCase(item.key))}</span>
        <strong>${escapeHtml(formatWhole.format(item.count))}</strong>
      </button>
    `).join('');
  }
  if (viewportEl) {
    viewportEl.innerHTML = `
      <div class="mapping-table-spacer" style="height:${view.totalHeight}px">
        <div class="mapping-table-window" style="transform: translateY(${view.start * view.rowHeight}px)">
          ${view.visibleRows.map((row) => `
            <button type="button" class="mapping-row ${state.mappingSelectedId === row.id ? 'is-selected' : ''}" data-mapping-select="${escapeHtml(row.id)}">
              <span class="mapping-row-title">${escapeHtml(row.title)}</span>
              <span class="mapping-row-venues">${escapeHtml(row.venues.join(' / '))}</span>
              <span class="mapping-row-status">${escapeHtml(titleCase(row.status))}</span>
              <span class="mapping-row-updated">${escapeHtml(relTime(row.freshness))}</span>
            </button>
          `).join('')}
        </div>
      </div>
    `;
  }
  if (inspectorEl) {
    inspectorEl.innerHTML = view.selected ? `
      <div class="mapping-inspector-card">
        <h3>${escapeHtml(view.selected.description || view.selected.title || view.selected.canonical_id)}</h3>
        <p class="mapping-inspector-copy">Review venue alignment, confidence, and operator posture without expanding the list.</p>
      </div>
    ` : emptyState('Select a mapping to inspect it.');
  }
}

document.addEventListener('click', (event) => {
  const mappingViewTarget = event.target.closest('[data-mapping-view]');
  if (mappingViewTarget) {
    state.mappingView = mappingViewTarget.getAttribute('data-mapping-view') || 'all';
    state.mappingSelectedId = '';
    state.mappingScrollTop = 0;
    renderMappings();
    return;
  }

  const mappingSelectTarget = event.target.closest('[data-mapping-select]');
  if (mappingSelectTarget) {
    state.mappingSelectedId = mappingSelectTarget.getAttribute('data-mapping-select') || '';
    renderMappings();
  }
});

if (mappingSearchInputEl) {
  mappingSearchInputEl.addEventListener('input', (event) => {
    state.mappingQuery = event.target.value || '';
    state.mappingScrollTop = 0;
    renderMappings();
  });
}

if (mappingViewportEl) {
  mappingViewportEl.addEventListener('scroll', (event) => {
    state.mappingScrollTop = event.target.scrollTop;
    renderMappings();
  });
}
```

- [ ] **Step 6: Add bounded workbench styling and viewport scrolling**

Update `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`.

```css
.mapping-workspace {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 320px;
  gap: 16px;
  min-height: 520px;
  max-height: min(72vh, 860px);
}

.mapping-sidebar,
.mapping-console,
.mapping-inspector {
  min-height: 0;
}

.mapping-sidebar,
.mapping-inspector,
.mapping-table-viewport {
  overflow: auto;
}

.mapping-table-viewport {
  position: relative;
  min-height: 0;
  border-radius: 18px;
  background: #111111;
}

.mapping-table-head,
.mapping-row {
  display: grid;
  grid-template-columns: minmax(0, 2.2fr) minmax(0, 1.3fr) 110px 90px;
  gap: 12px;
  align-items: center;
}

.mapping-row {
  width: 100%;
  min-height: 44px;
  padding: 0 14px;
  text-align: left;
  border: 0;
  background: transparent;
}

.mapping-row-title,
.mapping-row-venues {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 1120px) {
  .mapping-workspace {
    grid-template-columns: 1fr;
    max-height: none;
  }

  .mapping-inspector {
    order: 3;
  }
}
```

- [ ] **Step 7: Extend the dashboard audit for bounded mapping behavior and run it**

Add to `C:\Users\sande\Documents\arbiter-dashboard\output\verify_dashboard_polish.mjs`:

```js
const mappingFacts = await page.evaluate(() => {
  const workspace = document.querySelector('.mapping-workspace');
  const viewport = document.querySelector('#mappingTableViewport');
  return {
    workspaceHeight: workspace ? Math.round(workspace.getBoundingClientRect().height) : 0,
    viewportHeight: viewport ? Math.round(viewport.getBoundingClientRect().height) : 0,
    viewportScrollHeight: viewport ? Math.round(viewport.scrollHeight) : 0,
  };
});
```

Then run:

- `npx vitest run arbiter/web/mapping-workspace-model.test.js`
- `node output/verify_dashboard_polish.mjs`

Expected: PASS with mapping rows rendered inside a bounded pane and no new horizontal overflow.

- [ ] **Step 8: Commit**

```bash
git add arbiter/web/mapping-workspace-model.js arbiter/web/mapping-workspace-model.test.js arbiter/web/dashboard.js arbiter/web/styles.css arbiter/web/dashboard.html index.html output/verify_dashboard_polish.mjs
git commit -m "feat: add bounded mapping workspace ui"
```

---

### Task 5: Run the full UI verification sweep and fix the last responsive defects

**Files:**
- Modify only if the sweep exposes a defect:
  - `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\styles.css`
  - `C:\Users\sande\Documents\arbiter-dashboard\arbiter\web\dashboard.js`
  - `C:\Users\sande\Documents\arbiter-dashboard\output\verify_dashboard_polish.mjs`
  - `C:\Users\sande\Documents\arbiter-dashboard\output\verify_activity_atlas.mjs`
  - `C:\Users\sande\Documents\arbiter-dashboard\output\ui_verify.mjs`

- [ ] **Step 1: Run the focused frontend unit tests**

Run: `npx vitest run arbiter/web/dashboard-view-model.test.js arbiter/web/activity-atlas-model.test.js arbiter/web/mapping-workspace-model.test.js`

Expected: PASS.

- [ ] **Step 2: Run the JavaScript syntax check for the dashboard bundle**

Run: `node --check arbiter/web/dashboard.js`

Expected: PASS with no output.

- [ ] **Step 3: Run the dashboard layout audit across wide desktop, compact desktop, ops, and mobile**

Run: `node output/verify_dashboard_polish.mjs`

Expected: `Dashboard polish checks passed.`

- [ ] **Step 4: Run the Activity Atlas audit**

Run: `node output/verify_activity_atlas.mjs`

Expected: JSON output with no console/page/request errors, sticky header preserved, and no horizontal overflow.

- [ ] **Step 5: Run the full public/ops smoke verification**

Run: `node output/ui_verify.mjs`

Expected: JSON report where each scenario has `errors: []` and `facts.hasHorizontalOverflow: false`.

- [ ] **Step 6: If any script fails, fix only the surfaced UI defect and rerun the exact failing command before continuing**

Apply the smallest possible UI-only fix. Typical examples:

```css
@media (max-width: 640px) {
  .mapping-row {
    grid-template-columns: 1fr;
    gap: 6px;
    min-height: 56px;
    align-items: start;
  }

  .operator-meta-row {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 7: Commit the final clean verification state**

```bash
git add arbiter/web/styles.css arbiter/web/dashboard.js arbiter/web/dashboard.html index.html arbiter/web/activity-atlas-model.js arbiter/web/activity-atlas-model.test.js arbiter/web/mapping-workspace-model.js arbiter/web/mapping-workspace-model.test.js output/verify_dashboard_polish.mjs output/verify_activity_atlas.mjs output/ui_verify.mjs
git commit -m "fix: harden arbiter dashboard ui across breakpoints"
```


