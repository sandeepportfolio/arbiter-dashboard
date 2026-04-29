// Realistic mock data shaped like the actual Arbiter API responses.
// Backed by /api/health, /api/opportunities, /api/executions, /api/pnl,
// /api/deposits, /api/balances.

window.MOCK = (() => {
  const NOW = Math.floor(Date.now() / 1000);

  // ──────────────────────────────────────────────────────────────────────
  // Balances
  const balances = {
    kalshi: { balance: 487.42, timestamp: NOW - 12, is_low: false },
    polymarket: { balance: 612.08, timestamp: NOW - 8, is_low: false },
  };

  // ──────────────────────────────────────────────────────────────────────
  // P&L
  const pnl = {
    starting_balances: { kalshi: 500.00, polymarket: 500.00 },
    current_balances: { kalshi: 487.42, polymarket: 612.08 },
    total_deposits: { kalshi: 0, polymarket: 100.00 },
    total_deposits_all_platforms: 100.00,
    recorded_trading_pnl: { kalshi: -12.58, polymarket: 12.0843 },
    total_balance: 1099.50,
  };

  // ──────────────────────────────────────────────────────────────────────
  // Equity curve — last 24h, 5-min buckets
  const equity = [];
  let v = 1000;
  for (let i = 0; i < 288; i++) {
    const drift = (Math.sin(i / 18) * 0.4 + Math.cos(i / 7) * 0.2) * 0.5;
    const noise = (Math.random() - 0.48) * 1.4;
    v += drift + noise;
    if (i === 144) v += 100; // deposit bump
    equity.push({ t: NOW - (288 - i) * 300, v: +v.toFixed(2) });
  }

  // ──────────────────────────────────────────────────────────────────────
  // Opportunities
  const oppRows = [
    ['Will Trump win the 2028 GOP nomination?', 'TRUMP-2028-GOP-NOM', 'kalshi', 'polymarket', 0.62, 0.35, 4.8, 'tradable', 200, 9.6],
    ['Fed cuts rates at June 2026 meeting?', 'FED-JUNE-2026-CUT', 'polymarket', 'kalshi', 0.71, 0.27, 3.6, 'tradable', 150, 5.4],
    ['Bitcoin above $150K by end of 2026?', 'BTC-150K-EOY-2026', 'kalshi', 'polymarket', 0.41, 0.56, 4.2, 'tradable', 175, 7.35],
    ['Will GPT-6 release before Q4 2026?', 'GPT6-Q4-2026', 'polymarket', 'kalshi', 0.33, 0.64, 3.1, 'tradable', 120, 3.72],
    ['Senate balance after 2026 midterms?', 'SENATE-DEM-2026', 'kalshi', 'polymarket', 0.48, 0.50, 2.4, 'candidate', 90, 2.16],
    ['Will SpaceX IPO in 2026?', 'SPACEX-IPO-2026', 'polymarket', 'kalshi', 0.18, 0.79, 2.1, 'review', 80, 1.68],
    ['Tesla deliveries Q1 2026 over 500K?', 'TSLA-Q1-DEL-500K', 'kalshi', 'polymarket', 0.55, 0.42, 1.9, 'tradable', 100, 1.9],
    ['Will OpenAI sign DoD contract in 2026?', 'OAI-DOD-2026', 'polymarket', 'kalshi', 0.27, 0.70, 1.6, 'candidate', 75, 1.2],
    ['ETH above $8K by end of 2026?', 'ETH-8K-EOY-2026', 'kalshi', 'polymarket', 0.36, 0.61, 1.5, 'tradable', 90, 1.35],
    ['Russia ceasefire signed by July 2026?', 'RU-CEASE-JUL-2026', 'polymarket', 'kalshi', 0.22, 0.74, 1.2, 'review', 60, 0.72],
    ['Will Apple ship AR glasses in 2026?', 'AAPL-AR-2026', 'kalshi', 'polymarket', 0.31, 0.66, 1.1, 'candidate', 70, 0.77],
    ['NBA Finals — Celtics win 2026?', 'NBA-CELTICS-2026', 'polymarket', 'kalshi', 0.24, 0.73, 0.9, 'stale', 50, 0.45],
    ['Will US enter recession in 2026?', 'US-RECESSION-2026', 'kalshi', 'polymarket', 0.39, 0.58, 0.8, 'illiquid', 40, 0.32],
    ['Argentina inflation under 50% in 2026?', 'ARG-INFL-2026', 'polymarket', 'kalshi', 0.44, 0.54, 0.6, 'candidate', 30, 0.18],
  ];

  const opportunities = oppRows.map(([desc, cid, yp, np, yPrice, nPrice, edge, status, qty, profit], i) => ({
    description: desc,
    canonical_id: cid,
    yes_platform: yp,
    no_platform: np,
    yes_price: yPrice,
    no_price: nPrice,
    net_edge_cents: edge,
    net_edge: edge / 100,
    status,
    suggested_qty: qty,
    expected_profit: profit,
    yes_volume: 4000 + Math.floor(Math.random() * 12000),
    no_volume: 4000 + Math.floor(Math.random() * 12000),
    fee_rate_yes: yp === 'kalshi' ? 0.025 : 0.020,
    fee_rate_no: np === 'kalshi' ? 0.025 : 0.020,
    last_seen: NOW - i * 3,
    persistence_scans: 12 - i,
  }));

  // ──────────────────────────────────────────────────────────────────────
  // Trades / executions
  const tradeSeed = [
    ['ARB-09421', 'Will Trump win the 2028 GOP nomination?', 'TRUMP-2028-GOP-NOM', 'filled', 'filled', 'filled', 200, 0.62, 200, 0.35, 6.4124, NOW - 720],
    ['ARB-09418', 'Fed cuts rates at June 2026 meeting?', 'FED-JUNE-2026-CUT', 'filled', 'filled', 'filled', 150, 0.71, 150, 0.27, 4.1037, NOW - 1840],
    ['ARB-09415', 'Bitcoin above $150K by end of 2026?', 'BTC-150K-EOY-2026', 'filled', 'filled', 'filled', 175, 0.41, 175, 0.56, 5.0192, NOW - 3270],
    ['ARB-09412', 'Tesla deliveries Q1 2026 over 500K?', 'TSLA-Q1-DEL-500K', 'recovering', 'filled', 'submitted', 100, 0.55, 100, 0.42, 0, NOW - 4100],
    ['ARB-09408', 'Will GPT-6 release before Q4 2026?', 'GPT6-Q4-2026', 'filled', 'filled', 'filled', 120, 0.33, 120, 0.64, 2.4831, NOW - 5980],
    ['ARB-09405', 'ETH above $8K by end of 2026?', 'ETH-8K-EOY-2026', 'failed', 'filled', 'failed', 90, 0.36, 0, 0.61, -0.84, NOW - 7220],
    ['ARB-09401', 'Will Apple ship AR glasses in 2026?', 'AAPL-AR-2026', 'filled', 'filled', 'filled', 70, 0.31, 70, 0.66, 0.7129, NOW - 9460],
    ['ARB-09397', 'NBA Finals — Celtics win 2026?', 'NBA-CELTICS-2026', 'filled', 'filled', 'filled', 50, 0.24, 50, 0.73, 0.3187, NOW - 12300],
    ['ARB-09390', 'Argentina inflation under 50% in 2026?', 'ARG-INFL-2026', 'filled', 'filled', 'filled', 30, 0.44, 30, 0.54, 0.1592, NOW - 14820],
    ['ARB-09385', 'Will SpaceX IPO in 2026?', 'SPACEX-IPO-2026', 'filled', 'filled', 'filled', 80, 0.18, 80, 0.79, 1.0840, NOW - 18900],
  ];

  const executions = tradeSeed.map(([id, desc, cid, status, yStat, nStat, yQ, yP, nQ, nP, pnl_v, ts]) => ({
    arb_id: id,
    status,
    realized_pnl: pnl_v,
    timestamp: ts,
    opportunity: {
      description: desc,
      canonical_id: cid,
      yes_platform: cid.includes('FED') || cid.includes('GPT') || cid.includes('SPACEX') || cid.includes('OAI') || cid.includes('RU-') || cid.includes('NBA') || cid.includes('ARG') ? 'polymarket' : 'kalshi',
      no_platform: cid.includes('FED') || cid.includes('GPT') || cid.includes('SPACEX') || cid.includes('OAI') || cid.includes('RU-') || cid.includes('NBA') || cid.includes('ARG') ? 'kalshi' : 'polymarket',
    },
    leg_yes: { status: yStat, fill_qty: yQ, fill_price: yP },
    leg_no:  { status: nStat, fill_qty: nQ, fill_price: nP },
  }));

  // ──────────────────────────────────────────────────────────────────────
  // Deposits
  const deposits = {
    total_all: 100.00,
    deposits: [
      { type: 'deposit', amount: 100.00, platform: 'polymarket', timestamp: NOW - 86400 * 2 },
      { type: 'deposit', amount: 500.00, platform: 'kalshi',     timestamp: NOW - 86400 * 9 },
      { type: 'deposit', amount: 500.00, platform: 'polymarket', timestamp: NOW - 86400 * 9 },
    ],
  };

  // ──────────────────────────────────────────────────────────────────────
  // Health
  const scanHistory = [];
  for (let i = 0; i < 60; i++) {
    scanHistory.push({
      timestamp: NOW - (60 - i) * 5,
      best_edge_cents: Math.max(0, 1.2 + Math.sin(i / 4) * 2 + Math.random() * 1.6),
      active: 8 + Math.floor(Math.random() * 8),
      tradable: 2 + Math.floor(Math.random() * 5),
      scan_time_ms: 180 + Math.random() * 90,
    });
  }
  const last = scanHistory[scanHistory.length - 1];

  const health = {
    status: 'ok',
    mode: 'live',
    live_trading_ready: true,
    uptime_seconds: 9 * 3600 + 14 * 60,
    scanner: {
      scan_count: 18420,
      published: 312,
      scan_time_ms: 214,
      history: scanHistory,
    },
    execution: {
      total_executions: 87,
      filled: 81,
      failed: 4,
      recovering: 2,
    },
    audit: { audit_score: 0.9982 },
    profitability: { verdict: 'profitable', cumulative_pnl: -0.4957 },
    reconciliation: { reconciliation_count: 1240, flag_count: 0 },
    readiness: { ready: true, gates: 7, passing: 7 },
  };

  // ──────────────────────────────────────────────────────────────────────
  // Edge histogram (current)
  const edgeBuckets = [
    { range: '0-1¢',  count: 142 },
    { range: '1-2¢',  count: 86  },
    { range: '2-3¢',  count: 41  },
    { range: '3-4¢',  count: 18  },
    { range: '4-5¢',  count: 7   },
    { range: '5¢+',   count: 3   },
  ];

  return {
    NOW,
    balances,
    pnl,
    equity,
    opportunities,
    executions,
    deposits,
    health,
    edgeBuckets,
  };
})();
