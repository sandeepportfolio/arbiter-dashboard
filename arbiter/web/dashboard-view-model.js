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
  const validatorVerdict = titleCase(state.profitability?.verdict || state.system?.profitability?.verdict || "collecting_evidence");
  const positionCount = Number(state.portfolio?.total_open_positions || 0);

  return [
    {
      eyebrow: "Outcome",
      tag: totalPnl >= 0 ? "In range" : "Drawdown",
      tone: "tone-mint",
      label: "Realized P&L",
      value: formatUsd.format(totalPnl),
      meta: `${signedPercent((totalPnl / Math.max(totalExposure || 1, 1)) * 100)} vs open notional`,
    },
    {
      eyebrow: "Risk",
      tag: positionCount ? `${formatWhole.format(positionCount)} open` : "Idle",
      tone: "tone-blue",
      label: "Open exposure",
      value: formatUsd.format(totalExposure),
      meta: `${formatWhole.format(positionCount)} active positions across venues`,
    },
    {
      eyebrow: "Readiness",
      tag: validatorVerdict,
      tone: "tone-gold",
      label: "Validator state",
      value: `${Math.round(progress * 100)}%`,
      meta: `${validatorVerdict} with ${formatWhole.format(activeRoutes)} tradable routes`,
    },
    {
      eyebrow: "Flow",
      tag: bestEdge > 0 ? cents(bestEdge) : "Scanning",
      tone: "tone-plum",
      label: "Execution flow",
      value: formatWhole.format(totalExecutions),
      meta: `${cents(bestEdge)} best live edge in the current scan window`,
    },
  ];
}

function formatCooldown(sec) {
  if (!Number.isFinite(sec) || sec <= 0) return null;
  const mm = String(Math.floor(sec / 60)).padStart(2, "0");
  const ss = String(Math.floor(sec % 60)).padStart(2, "0");
  return `${mm}:${ss}`;
}

export function buildSafetyView(state, options = {}) {
  const now = Number(options.nowTimestamp ?? Date.now() / 1000);
  const ks = state?.safety?.killSwitch ?? { armed: false };
  const cooldownRemaining = Math.max(0, Number(ks.cooldown_until ?? 0) - now);
  const armed = Boolean(ks.armed);
  return {
    armed,
    badgeLabel: armed ? "ARMED" : "Disarmed",
    badgeClass: armed ? "status-critical" : "status-ok",
    summary: armed
      ? `Armed by ${ks.armed_by || "unknown"} — ${ks.armed_reason || "no reason"}`
      : "Kill switch disarmed. Armed state cancels open orders.",
    armedBy: ks.armed_by || null,
    armedAt: Number(ks.armed_at || 0) || null,
    armedReason: ks.armed_reason || null,
    cooldownRemainingSeconds: cooldownRemaining,
    canReset: Boolean(armed && cooldownRemaining <= 0),
    cooldownLabel: formatCooldown(cooldownRemaining),
  };
}

export function buildRateLimitView(state) {
  const limits = state?.safety?.rateLimits ?? {};
  return Object.entries(limits).map(([platform, stats]) => {
    const remainingPenalty = Number(stats?.remaining_penalty_seconds ?? 0);
    const available = Number(stats?.available_tokens ?? 0);
    const max = Number(stats?.max_requests ?? 0);
    let tone = "ok";
    if (remainingPenalty > 0) tone = "warn";
    else if (available === 0 && max > 0) tone = "warn";
    // "crit" reserved for circuit-open state (future; not covered in this phase)
    return {
      platform,
      platformLabel:
        platform === "kalshi"
          ? "Kalshi"
          : platform === "polymarket"
            ? "Polymarket"
            : titleCase(platform),
      tokensLabel: `${available}/${max}`,
      tone,
      cooldownLabel: remainingPenalty > 0 ? `${remainingPenalty.toFixed(1)}s cooldown` : "idle",
      remainingPenaltySeconds: remainingPenalty,
      availableTokens: available,
      maxRequests: max,
    };
  });
}

export function buildMappingComparison(mapping) {
  const rc = mapping?.resolution_criteria;
  const status = mapping?.resolution_match_status ?? rc?.criteria_match ?? "pending_operator_review";
  const chipToneByStatus = {
    identical: "ok",
    similar: "warn",
    divergent: "crit",
    pending_operator_review: "warn",
  };
  const hasData = rc !== null && rc !== undefined;
  const chipLabel =
    status === "pending_operator_review"
      ? hasData
        ? "Pending operator review"
        : "Criteria missing"
      : typeof status === "string" && status.length
        ? status.charAt(0).toUpperCase() + status.slice(1)
        : "Unknown";
  return {
    canonicalId: mapping?.canonical_id,
    hasData,
    kalshiRule: rc?.kalshi?.rule ?? null,
    kalshiSource: rc?.kalshi?.source ?? null,
    kalshiSettlement: rc?.kalshi?.settlement_date ?? null,
    polymarketRule: rc?.polymarket?.rule ?? null,
    polymarketSource: rc?.polymarket?.source ?? null,
    polymarketSettlement: rc?.polymarket?.settlement_date ?? null,
    operatorNote: rc?.operator_note ?? "",
    matchStatus: status,
    chipTone: chipToneByStatus[status] ?? "warn",
    chipLabel,
    canConfirm: status === "identical" || status === "similar",
  };
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
