#!/usr/bin/env python3
"""Build production ../ops.html from the redesign sources.

Strategy:
  - Inline theme.js + mock-data.js as plain <script>.
  - Inline an API integration layer that fetches real /api/* endpoints
    into window.MOCK with mock-data fallback.
  - Inline each component .jsx file as its own <script type="text/babel">
    block to preserve per-file scope (matches the design's loading model).
  - Skip design-time files (design-canvas, ios-frame, tweaks).
  - Replace the design canvas mount with a viewport-aware App that picks
    DesktopApp (>768px) vs MobileApp (≤768px), and a 10s polling refresh.

Run from anywhere — paths resolve relative to this script.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent
OUT = SRC.parent / "ops.html"

# Order matters: dependents come after dependencies. Mirror the order from
# the design's Arbiter_Redesign.html (minus design-canvas/ios-frame/tweaks).
COMPONENT_FILES = [
    "charts.jsx",
    "ui-primitives.jsx",
    "app-shell.jsx",
    "page-overview.jsx",
    "page-opportunities.jsx",
    "pages-rest.jsx",
    "modals.jsx",
    "actions.jsx",
    "agent-validate.jsx",
    "mobile.jsx",
    "login.jsx",
]


HEAD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arbiter — Operator desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  html, body { margin: 0; padding: 0; background: #FAFAF9; font-family: "Inter", system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  html, body, #root { height: 100%; }
  * { box-sizing: border-box; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes cliBlink { 0%, 50% { opacity: 1; } 50.01%, 100% { opacity: 0; } }
  @keyframes toastIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(127,127,127,0.25); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(127,127,127,0.4); }
  button { font-family: inherit; }
  input { font-family: inherit; }
  /* Loading splash, replaced once React mounts. */
  #boot-splash { position: fixed; inset: 0; display:flex; align-items:center; justify-content:center; flex-direction:column; gap: 14px; background:#FAFAF9; color:#6B6B66; font-family: "Inter", system-ui, sans-serif; }
  #boot-splash .ring { width: 28px; height: 28px; border:2px solid #E8E6E0; border-top-color:#5B5BD6; border-radius: 50%; animation: spin 0.8s linear infinite; }
  @media (prefers-color-scheme: dark) {
    html, body, #boot-splash { background:#0B0C10; color:#8B92A0; }
    #boot-splash .ring { border-color:#22262F; border-top-color:#8B8DF5; }
  }
</style>
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js" crossorigin="anonymous"></script>
<script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js" crossorigin="anonymous"></script>
<script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js" crossorigin="anonymous"></script>
"""


API_LAYER = r"""<script>
// ─────────────────────────────────────────────────────────────────────
// Arbiter API integration layer
//
// Fetches real data from /api/* endpoints, normalises into the same
// shape as window.MOCK (defined in mock-data.js), and falls back to the
// mock values on failure. The function mutates window.MOCK in place so
// every component picking it up at render time gets fresh values.
// ─────────────────────────────────────────────────────────────────────
(() => {
  const API = window.location.origin;

  async function safeJson(path, opts) {
    try {
      const r = await fetch(API + path, Object.assign({ credentials: 'same-origin' }, opts || {}));
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  function asNumber(x, fallback) {
    const n = Number(x);
    return Number.isFinite(n) ? n : fallback;
  }

  function mergeBalances(M, balances) {
    if (!balances || typeof balances !== 'object') return;
    for (const platform of ['kalshi', 'polymarket']) {
      const b = balances[platform];
      if (b && typeof b === 'object') {
        M.balances[platform] = {
          balance: asNumber(b.balance, M.balances[platform].balance),
          timestamp: asNumber(b.timestamp, M.balances[platform].timestamp),
          is_low: !!b.is_low,
        };
      }
    }
  }

  function mergePnl(M, pnl) {
    if (!pnl || typeof pnl !== 'object') return;
    if (pnl.starting_balances) M.pnl.starting_balances = Object.assign({}, M.pnl.starting_balances, pnl.starting_balances);
    if (pnl.current_balances)  M.pnl.current_balances  = Object.assign({}, M.pnl.current_balances,  pnl.current_balances);
    if (pnl.total_deposits)    M.pnl.total_deposits    = Object.assign({}, M.pnl.total_deposits,    pnl.total_deposits);
    if (pnl.recorded_trading_pnl) M.pnl.recorded_trading_pnl = Object.assign({}, M.pnl.recorded_trading_pnl, pnl.recorded_trading_pnl);
    if (pnl.total_balance != null) M.pnl.total_balance = asNumber(pnl.total_balance, M.pnl.total_balance);
    if (pnl.total_deposits_all_platforms != null) M.pnl.total_deposits_all_platforms = asNumber(pnl.total_deposits_all_platforms, M.pnl.total_deposits_all_platforms);
  }

  function mergeOpportunities(M, opps) {
    if (!Array.isArray(opps) || opps.length === 0) return;
    M.opportunities = opps.map((o) => {
      const yes = o.yes_platform || 'kalshi';
      const no = o.no_platform || 'polymarket';
      const yesPrice = asNumber(o.yes_price, 0.5);
      const noPrice = asNumber(o.no_price, 0.5);
      const edgeCents = asNumber(o.net_edge_cents, asNumber(o.net_edge, 0) * 100);
      const qty = asNumber(o.suggested_qty, asNumber(o.max_quantity, 100));
      return {
        description: o.description || o.market_name || o.canonical_id || 'Untitled market',
        canonical_id: o.canonical_id || o.id || 'UNKNOWN',
        yes_platform: yes,
        no_platform: no,
        yes_price: yesPrice,
        no_price: noPrice,
        net_edge_cents: edgeCents,
        net_edge: edgeCents / 100,
        status: o.status || 'tradable',
        suggested_qty: qty,
        expected_profit: asNumber(o.expected_profit, edgeCents * qty / 100),
        yes_volume: asNumber(o.yes_volume, 5000),
        no_volume: asNumber(o.no_volume, 5000),
        fee_rate_yes: asNumber(o.fee_rate_yes, yes === 'kalshi' ? 0.025 : 0.020),
        fee_rate_no:  asNumber(o.fee_rate_no,  no  === 'kalshi' ? 0.025 : 0.020),
        last_seen: asNumber(o.last_seen, Math.floor(Date.now() / 1000)),
        persistence_scans: asNumber(o.persistence_scans, asNumber(o.persistence_count, 1)),
        persistence_count: asNumber(o.persistence_count, asNumber(o.persistence_scans, 1)),
      };
    });
  }

  function mergeExecutions(M, execs) {
    if (!Array.isArray(execs) || execs.length === 0) return;
    M.executions = execs.map((e) => {
      const opp = e.opportunity || {};
      const yp = opp.yes_platform || e.yes_platform || 'kalshi';
      const np = opp.no_platform || e.no_platform || 'polymarket';
      return {
        arb_id: e.arb_id || e.id || 'ARB-?',
        status: e.status || 'filled',
        realized_pnl: asNumber(e.realized_pnl, 0),
        timestamp: asNumber(e.timestamp, Math.floor(Date.now() / 1000)),
        opportunity: {
          description: opp.description || e.description || opp.canonical_id || e.canonical_id || 'Trade',
          canonical_id: opp.canonical_id || e.canonical_id || 'UNKNOWN',
          yes_platform: yp,
          no_platform: np,
        },
        leg_yes: {
          status: (e.leg_yes && e.leg_yes.status) || 'filled',
          fill_qty: asNumber(e.leg_yes && e.leg_yes.fill_qty, 0),
          fill_price: asNumber(e.leg_yes && e.leg_yes.fill_price, 0),
        },
        leg_no: {
          status: (e.leg_no && e.leg_no.status) || 'filled',
          fill_qty: asNumber(e.leg_no && e.leg_no.fill_qty, 0),
          fill_price: asNumber(e.leg_no && e.leg_no.fill_price, 0),
        },
      };
    });
  }

  function mergeDeposits(M, deps) {
    if (!deps || typeof deps !== 'object') return;
    if (Array.isArray(deps.deposits) && deps.deposits.length) {
      M.deposits.deposits = deps.deposits.map((d) => ({
        type: d.type || 'deposit',
        amount: asNumber(d.amount, 0),
        platform: d.platform || 'kalshi',
        timestamp: asNumber(d.timestamp, Math.floor(Date.now() / 1000)),
      }));
    }
    if (deps.total_all != null) M.deposits.total_all = asNumber(deps.total_all, M.deposits.total_all);
  }

  function mergeHealth(M, health) {
    if (!health || typeof health !== 'object') return;
    if (health.status) M.health.status = health.status;
    if (health.uptime_seconds != null) M.health.uptime_seconds = asNumber(health.uptime_seconds, M.health.uptime_seconds);
    if (health.live_trading_ready != null) M.health.live_trading_ready = !!health.live_trading_ready;
    if (health.scanner) {
      const s = health.scanner;
      M.health.scanner.scan_count = asNumber(s.scan_count, M.health.scanner.scan_count);
      M.health.scanner.published = asNumber(s.published, M.health.scanner.published);
      M.health.scanner.scan_time_ms = asNumber(s.scan_time_ms, M.health.scanner.scan_time_ms);
      // Keep history if backend doesn't expose one (dashboard chart needs data).
      if (Array.isArray(s.history) && s.history.length) M.health.scanner.history = s.history;
    }
    if (health.execution) {
      const x = health.execution;
      M.health.execution.total_executions = asNumber(x.total_executions, M.health.execution.total_executions);
      M.health.execution.filled = asNumber(x.filled, M.health.execution.filled);
      M.health.execution.failed = asNumber(x.failed, M.health.execution.failed);
      M.health.execution.recovering = asNumber(x.recovering, M.health.execution.recovering);
    }
    if (health.audit && health.audit.audit_score != null) {
      M.health.audit.audit_score = asNumber(health.audit.audit_score, M.health.audit.audit_score);
    }
    if (health.profitability) {
      if (health.profitability.verdict) M.health.profitability.verdict = health.profitability.verdict;
      if (health.profitability.cumulative_pnl != null) M.health.profitability.cumulative_pnl = asNumber(health.profitability.cumulative_pnl, M.health.profitability.cumulative_pnl);
    }
    if (health.reconciliation) {
      if (health.reconciliation.reconciliation_count != null) M.health.reconciliation.reconciliation_count = asNumber(health.reconciliation.reconciliation_count, M.health.reconciliation.reconciliation_count);
      if (health.reconciliation.flag_count != null) M.health.reconciliation.flag_count = asNumber(health.reconciliation.flag_count, M.health.reconciliation.flag_count);
    }
    if (health.readiness) {
      if (health.readiness.gates != null) M.health.readiness.gates = asNumber(health.readiness.gates, M.health.readiness.gates);
      if (health.readiness.passing != null) M.health.readiness.passing = asNumber(health.readiness.passing, M.health.readiness.passing);
      if (health.readiness.ready_for_live_trading != null) M.health.readiness.ready = !!health.readiness.ready_for_live_trading;
      else if (health.readiness.ready != null) M.health.readiness.ready = !!health.readiness.ready;
    }
  }

  function mergeMappings(M, rows) {
    if (!Array.isArray(rows) || rows.length === 0) return;
    // Mappings are read by PageMappings via a hardcoded list today; expose
    // the live data on M.mappings for any future component that wants it.
    M.mappings = rows;
  }

  async function refresh() {
    const M = window.MOCK;
    if (!M) return;
    const [health, balances, opps, execs, pnl, deposits, mappings] = await Promise.all([
      safeJson('/api/health'),
      safeJson('/api/balances'),
      safeJson('/api/opportunities'),
      safeJson('/api/executions'),
      safeJson('/api/pnl'),
      safeJson('/api/deposits'),
      safeJson('/api/mappings'),
    ]);
    mergeHealth(M, health);
    mergeBalances(M, balances);
    mergeOpportunities(M, opps);
    mergeExecutions(M, execs);
    mergePnl(M, pnl);
    mergeDeposits(M, deposits);
    mergeMappings(M, mappings);
    M.lastFetch = Date.now();
  }

  // ── Operator actions (write endpoints) ──────────────────────────────
  async function killSwitch(state) {
    try {
      const r = await fetch(API + '/api/kill-switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ engaged: !!state }),
      });
      return r.ok;
    } catch { return false; }
  }

  async function retryFailedTrade(arbId) {
    try {
      const r = await fetch(API + '/api/failed-trades/' + encodeURIComponent(arbId) + '/retry', {
        method: 'POST',
        credentials: 'same-origin',
      });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }

  window.__arbiterRefresh = refresh;
  window.__arbiterKillSwitch = killSwitch;
  window.__arbiterRetry = retryFailedTrade;
})();
</script>
"""


FOOTER = r"""<script type="text/babel" data-presets="env,react">
  // ─────────────────────────────────────────────────────────────────────
  // Top-level App: viewport-aware, polls the API every 10s and re-renders.
  // ─────────────────────────────────────────────────────────────────────
  const { useState, useEffect } = React;

  function PageRouter() {
    const { page, t } = window.useApp();
    const map = {
      overview: window.PageOverview,
      opportunities: window.PageOpportunities,
      trades: window.PageTrades,
      pnl: window.PagePnL,
      markets: window.PageMarkets,
      mappings: window.PageMappings,
      scanner: window.PageScanner,
      audit: window.PageAudit,
      deposits: window.PageDeposits,
      settings: window.PageSettings,
    };
    const tradableCount = (window.MOCK.opportunities || []).filter(o => o.status === 'tradable').length;
    const titles = {
      overview: ['Overview', 'Operator desk · Live'],
      opportunities: ['Opportunities', `Live scan · ${tradableCount} tradable`],
      trades: ['Trades', `Execution ledger · ${(window.MOCK.executions || []).length} trades`],
      pnl: ['P&L', 'Performance · since inception'],
      markets: ['Markets', `Mapped universe · ${(window.MOCK.mappings || window.MOCK.opportunities || []).length} markets`],
      mappings: ['Mappings', 'Cross-platform · review queue'],
      scanner: ['Scanner', `Real-time · ${(window.MOCK.health.scanner.scan_count || 0).toLocaleString()} scans`],
      audit: ['Audit & Readiness', `System integrity · ${window.MOCK.health.readiness.passing || 0}/${window.MOCK.health.readiness.gates || 7} gates`],
      deposits: ['Funds', 'Capital · 2 platforms connected'],
      settings: ['Settings', 'Configuration'],
    };
    const Page = map[page] || window.PageOverview;
    const [title, sub] = titles[page] || titles.overview;
    return (
      <div style={{ display:'flex', height:'100%', background: t.bg, overflow:'hidden' }}>
        <window.Sidebar/>
        <div style={{ flex: 1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
          <window.TopBar title={title} sub={sub}/>
          <div style={{ flex: 1, overflow:'auto' }}>
            <Page/>
          </div>
        </div>
      </div>
    );
  }

  function DesktopShell() {
    const { t } = window.useApp();
    return (
      <div style={{ width: '100%', height: '100%', background: t.bg, color: t.text, fontFamily: window.FONTS.sans, position:'relative' }}>
        <PageRouter/>
        <window.OppDrawer/>
        <window.Modal/>
        <window.ExtraModal/>
        <window.AgentValidateModal/>
        <window.RefetchMappingsModal/>
        <window.CommandPalette/>
        <window.AlertsInbox/>
        <window.UserMenu/>
        <window.ConfirmDialog/>
        <window.ToastHost/>
      </div>
    );
  }

  function DesktopApp() {
    return (
      <window.AppProvider>
        <DesktopShell/>
      </window.AppProvider>
    );
  }

  function MobileApp() {
    return (
      <window.AppProvider>
        <window.MobileDashboard/>
        <window.Modal/>
        <window.ExtraModal/>
        <window.AgentValidateModal/>
        <window.RefetchMappingsModal/>
        <window.ConfirmDialog/>
        <window.ToastHost/>
      </window.AppProvider>
    );
  }

  function App() {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth <= 768);
    const [, setTick] = useState(0);

    // Track viewport size; re-render between desktop and mobile shells.
    useEffect(() => {
      const onResize = () => setIsMobile(window.innerWidth <= 768);
      window.addEventListener('resize', onResize);
      return () => window.removeEventListener('resize', onResize);
    }, []);

    // Initial fetch + 10s polling. On each refresh, bump tick so the tree
    // re-renders and reads the freshly-mutated window.MOCK.
    useEffect(() => {
      let cancelled = false;
      const tick = async () => {
        if (window.__arbiterRefresh) {
          try { await window.__arbiterRefresh(); } catch {}
        }
        if (!cancelled) setTick(x => x + 1);
      };
      tick();
      const id = setInterval(tick, 10000);
      return () => { cancelled = true; clearInterval(id); };
    }, []);

    return isMobile ? <MobileApp/> : <DesktopApp/>;
  }

  function mount() {
    const required = [
      'AppProvider', 'Sidebar', 'TopBar',
      'PageOverview', 'PageOpportunities', 'PageTrades', 'PagePnL',
      'PageMarkets', 'PageMappings', 'PageScanner', 'PageAudit',
      'PageDeposits', 'PageSettings',
      'OppDrawer', 'Modal', 'ExtraModal', 'AgentValidateModal',
      'RefetchMappingsModal', 'CommandPalette', 'AlertsInbox', 'UserMenu',
      'ConfirmDialog', 'ToastHost', 'MobileDashboard',
      'AreaChart', 'Sparkline', 'BarChart', 'Donut',
      'Card', 'Stat', 'Pill', 'PlatformChip', 'Btn', 'DataTable', 'PageHeader',
    ];
    for (const k of required) {
      if (!window[k]) return setTimeout(mount, 50);
    }
    const splash = document.getElementById('boot-splash');
    if (splash) splash.remove();
    ReactDOM.createRoot(document.getElementById('root')).render(<App />);
  }
  mount();
</script>
"""


def jsx_block(name: str, content: str) -> str:
    """Wrap a JSX file's contents in a <script type='text/babel'> block.

    Each block is its own scope, so file-local declarations don't collide
    across files (matches the original design's loading model).
    """
    return (
        f"<script type=\"text/babel\" data-presets=\"env,react\" "
        f"data-filename=\"{name}\">\n{content}\n</script>\n"
    )


def main() -> None:
    parts: list[str] = [HEAD]

    # theme.js + mock-data.js — plain <script>, no transpilation needed.
    for js in ("theme.js", "mock-data.js"):
        body = (SRC / js).read_text()
        parts.append(f"<script>\n{body}\n</script>\n")

    parts.append(API_LAYER)

    for name in COMPONENT_FILES:
        body = (SRC / name).read_text()
        parts.append(jsx_block(name, body))

    parts.append("</head>\n<body>\n")
    parts.append(
        '<div id="root"></div>\n'
        '<div id="boot-splash"><div class="ring"></div>'
        '<div>Loading Arbiter…</div></div>\n'
    )
    parts.append(FOOTER)
    parts.append("</body>\n</html>\n")

    OUT.write_text("".join(parts))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
