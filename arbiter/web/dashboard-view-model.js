const formatUsd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const formatWhole = new Intl.NumberFormat("en-US");

function titleCase(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function platformLabel(platform) {
  const labels = {
    kalshi: "Kalshi",
    polymarket: "Polymarket",
    predictit: "PredictIt",
  };
  return labels[platform] || titleCase(platform);
}

function relTime(timestamp, nowTimestamp = Date.now() / 1000) {
  if (!timestamp) return "just now";
  const delta = Math.max(0, nowTimestamp - timestamp);
  if (delta < 10) return "just now";
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  return `${Math.round(delta / 3600)}h ago`;
}

function clamp01(value) {
  return Math.max(0, Math.min(1, Number(value || 0)));
}

function cents(value) {
  return `${Number(value || 0).toFixed(1)}\u00a2`;
}

function signedCurrency(value) {
  const amount = Number(value || 0);
  return `${amount >= 0 ? "+" : "-"}${formatUsd.format(Math.abs(amount))}`;
}

function signedPercent(value) {
  const amount = Number(value || 0);
  return `${amount >= 0 ? "+" : "-"}${Math.abs(amount).toFixed(1)}%`;
}

function countOpenIncidents(incidents = []) {
  return incidents.filter((incident) => incident.status !== "resolved").length;
}

function countLowBalances(balances = {}) {
  return Object.values(balances).filter((snapshot) => snapshot?.is_low).length;
}

function countCooldowns(collectors = {}) {
  return Object.values(collectors).filter((collector) => Number(collector?.rate_limiter?.remaining_penalty_seconds || 0) > 0).length;
}

function riskPercent({ portfolio, incidents, manualPositions, system }) {
  const auditPassRate = Number(system?.audit?.pass_rate || system?.execution?.audit?.pass_rate || 0);
  const openIncidents = countOpenIncidents(incidents);
  const violations = portfolio?.violations?.length || 0;
  const lowBalances = countLowBalances(system?.balances || {});
  const manualQueue = manualPositions?.length || 0;
  const cooldowns = countCooldowns(system?.collectors || {});
  const auditPenalty = Math.max(0, 0.995 - auditPassRate) * 400;

  return Math.round(Math.min(
    100,
    (openIncidents * 26)
      + (violations * 18)
      + (lowBalances * 12)
      + (manualQueue * 10)
      + (cooldowns * 6)
      + auditPenalty
  ));
}

function riskSummary(percent, stats) {
  if (percent >= 70) {
    return `Risk posture is elevated. ${stats.openIncidents} open incidents and ${stats.violations} portfolio warnings should be cleared before scaling new routes.`;
  }
  if (percent >= 40) {
    return `Risk posture is watched. Manual queue pressure or funding exceptions are still present in the desk.`;
  }
  return "Risk posture is controlled. Guardrails are holding and the desk can stay focused on fresh opportunities.";
}

function recentTradeAccent(trade) {
  const status = String(trade?.status || "").toLowerCase();
  if (status.includes("failed") || status.includes("cancelled")) return "negative";
  const pnl = Number(trade?.realized_pnl || 0);
  if (pnl > 0) return "positive";
  if (pnl < 0) return "negative";
  return "neutral";
}

export function buildDeskOverview(state, options = {}) {
  const nowTimestamp = Number(options.nowTimestamp || Date.now() / 1000);
  const equitySeries = state.system?.series?.equity || [];
  const latestEquity = equitySeries[equitySeries.length - 1]?.equity;
  const firstEquity = equitySeries[0]?.equity;
  const heroValueNumber = latestEquity ?? state.system?.execution?.total_pnl ?? 0;
  const heroDeltaNumber = (latestEquity != null && firstEquity != null)
    ? latestEquity - firstEquity
    : state.system?.execution?.total_pnl ?? 0;
  const updatedAt = state.lastQuoteAt || equitySeries[equitySeries.length - 1]?.timestamp || state.system?.timestamp || 0;
  const percent = riskPercent(state);
  const stats = {
    openIncidents: countOpenIncidents(state.incidents),
    violations: state.portfolio?.violations?.length || 0,
    manualQueue: state.manualPositions?.length || 0,
    lowBalances: countLowBalances(state.system?.balances || {}),
    cooldowns: countCooldowns(state.system?.collectors || {}),
    auditPassRate: Number(state.system?.audit?.pass_rate || state.system?.execution?.audit?.pass_rate || 0),
  };

  const recentTrades = [...(state.trades || [])]
    .sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0))
    .slice(0, 4)
    .map((trade) => {
      const quantity = Math.max(trade.leg_yes?.quantity || 0, trade.leg_no?.quantity || 0);
      const accent = recentTradeAccent(trade);
      return {
        id: trade.arb_id,
        title: trade.opportunity?.description || trade.opportunity?.canonical_id || trade.arb_id,
        status: titleCase(trade.status || "pending"),
        timestampLabel: relTime(trade.timestamp, nowTimestamp),
        route: `${platformLabel(trade.leg_yes?.platform)} / ${platformLabel(trade.leg_no?.platform)}`,
        value: signedCurrency(trade.realized_pnl || 0),
        accent,
        copy: `${formatWhole.format(quantity)} contracts`,
      };
    });

  return {
    heroValue: formatUsd.format(heroValueNumber || 0),
    heroDelta: signedCurrency(heroDeltaNumber || 0),
    heroUpdated: `Updated ${relTime(updatedAt, nowTimestamp)}`,
    risk: {
      percent,
      summary: riskSummary(percent, stats),
      updatedLabel: `Updated\n${relTime(updatedAt, nowTimestamp)}`,
      items: [
        { label: "Open incidents", copy: `${formatWhole.format(stats.openIncidents)} active recovery cases` },
        { label: "Risk warnings", copy: `${formatWhole.format(stats.violations)} portfolio violations` },
        { label: "Manual queue", copy: `${formatWhole.format(stats.manualQueue)} items waiting for operator action` },
        { label: "Funding alerts", copy: `${formatWhole.format(stats.lowBalances)} low-balance venues and ${formatWhole.format(stats.cooldowns)} collector cooldowns` },
        { label: "Math audit", copy: `${(stats.auditPassRate * 100).toFixed(1)}% pass rate` },
      ],
    },
    recentTrades,
  };
}

export function buildMetricCards(state) {
  const totalPnl = Number(state.system?.execution?.total_pnl || 0);
  const totalExposure = Number(state.portfolio?.total_exposure || 0);
  const progress = Number(state.profitability?.progress || state.system?.profitability?.progress || 0);
  const totalExecutions = Number(state.system?.execution?.total_executions || 0);
  const activeRoutes = Number(state.system?.scanner?.tradable_opportunities || 0);
  const bestEdge = Number(state.system?.scanner?.best_edge_cents || 0);

  return [
    {
      label: "Realized P&L",
      value: formatUsd.format(totalPnl),
      meta: `${signedPercent((totalPnl / Math.max(totalExposure || 1, 1)) * 100)} vs open notional`,
    },
    {
      label: "Open exposure",
      value: formatUsd.format(totalExposure),
      meta: `${formatWhole.format(state.portfolio?.total_open_positions || 0)} active positions across venues`,
    },
    {
      label: "Validator progress",
      value: `${Math.round(progress * 100)}%`,
      meta: `${titleCase(state.profitability?.verdict || state.system?.profitability?.verdict || "collecting_evidence")} with ${formatWhole.format(activeRoutes)} tradable routes`,
    },
    {
      label: "Trade throughput",
      value: formatWhole.format(totalExecutions),
      meta: `${cents(bestEdge)} best live edge in the current scan window`,
    },
  ];
}

export function buildOpportunityRows({ opportunities = [], system = {}, nowTimestamp = Date.now() / 1000 }) {
  const scanner = system?.scanner || {};
  const maxQuoteAgeSeconds = Number(scanner.max_quote_age_seconds || 15);
  const persistenceScans = Number(scanner.persistence_scans || 0);
  const priority = {
    tradable: 0,
    manual: 1,
    review: 2,
    candidate: 3,
    stale: 4,
    illiquid: 5,
  };

  return [...opportunities]
    .sort((left, right) => {
      const leftPriority = priority[left.status] ?? 6;
      const rightPriority = priority[right.status] ?? 6;
      if (leftPriority !== rightPriority) return leftPriority - rightPriority;
      if (Number(right.net_edge_cents || 0) !== Number(left.net_edge_cents || 0)) {
        return Number(right.net_edge_cents || 0) - Number(left.net_edge_cents || 0);
      }
      return Number(right.timestamp || 0) - Number(left.timestamp || 0);
    })
    .map((opp) => {
      const freshness = clamp01(1 - (Number(opp.quote_age_seconds || 0) / Math.max(maxQuoteAgeSeconds, 1)));
      return {
        id: `${opp.canonical_id}-${opp.yes_platform}-${opp.no_platform}`,
        canonicalId: opp.canonical_id,
        title: opp.description || opp.canonical_id,
        status: String(opp.status || "candidate"),
        statusLabel: titleCase(opp.status || "candidate"),
        route: `${platformLabel(opp.yes_platform)} YES -> ${platformLabel(opp.no_platform)} NO`,
        netEdgeLabel: cents(opp.net_edge_cents || 0),
        maxProfitLabel: formatUsd.format(opp.max_profit_usd || 0),
        confidenceLabel: `${Math.round(clamp01(opp.confidence || 0) * 100)}%`,
        freshnessLabel: `${Math.round(freshness * 100)}%`,
        scansLabel: `${formatWhole.format(opp.persistence_count || 0)}/${formatWhole.format(persistenceScans)}`,
        quantityLabel: formatWhole.format(opp.suggested_qty || 0),
        liquidityLabel: formatWhole.format(Math.round(opp.min_available_liquidity || 0)),
        updatedLabel: relTime(opp.timestamp, nowTimestamp),
      };
    });
}
