const state = {
  system: null,
  opportunities: [],
  trades: [],
  manualPositions: [],
  incidents: [],
  mappings: [],
  wsConnected: false,
  lastQuoteAt: null,
  activeLogFilter: "all",
};

const LOG_DEFINITIONS = {
  market: {
    label: "Market Pulse",
    description: "Quote heartbeats, tracked markets, and stream freshness.",
    source: "Published live",
    tone: "tone-mint",
  },
  opportunity: {
    label: "Scanner",
    description: "Fee-positive opportunity evaluations and readiness states.",
    source: "Published live + tracked",
    tone: "tone-mint",
  },
  execution: {
    label: "Execution",
    description: "Simulated, submitted, filled, and failed hedge attempts.",
    source: "Published live + tracked",
    tone: "tone-gold",
  },
  manual: {
    label: "Manual Flow",
    description: "PredictIt-assisted workflows that need operator attention.",
    source: "Tracked",
    tone: "tone-plum",
  },
  incident: {
    label: "Recovery",
    description: "Slippage, stale quotes, and one-leg risk handling.",
    source: "Published live + tracked",
    tone: "tone-rose",
  },
  balance: {
    label: "Balance",
    description: "Funding posture and low-balance trade blockers.",
    source: "Tracked",
    tone: "tone-amber",
  },
  collector: {
    label: "Collectors",
    description: "Venue polling, circuit breakers, and data feed stability.",
    source: "Tracked",
    tone: "tone-blue",
  },
  mapping: {
    label: "Mapping",
    description: "Cross-market identity and auto-trade readiness.",
    source: "Tracked",
    tone: "tone-slate",
  },
};

const FILTER_ORDER = ["all", "market", "opportunity", "execution", "manual", "incident", "balance", "collector", "mapping"];

const PUBLISHED_TYPES = [
  { key: "system", label: "system", detail: "bootstrap + snapshot" },
  { key: "quote", label: "quote", detail: "market pulse" },
  { key: "opportunity", label: "opportunity", detail: "scanner publish" },
  { key: "execution", label: "execution", detail: "trade update" },
  { key: "incident", label: "incident", detail: "recovery event" },
  { key: "heartbeat", label: "heartbeat", detail: "ws keepalive" },
];

const TRACKED_TYPES = [
  { key: "opportunities", label: "opportunities" },
  { key: "trades", label: "trades" },
  { key: "manualPositions", label: "manual queue" },
  { key: "incidents", label: "incidents" },
  { key: "balances", label: "balances" },
  { key: "collectors", label: "collectors" },
  { key: "mappings", label: "mappings" },
];

const edgeChartEl = document.getElementById("edgeChart");
const equityChartEl = document.getElementById("equityChart");
const wsStatusEl = document.getElementById("wsStatus");
const modePillEl = document.getElementById("modePill");

const formatUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const formatWhole = new Intl.NumberFormat("en-US");
const formatClock = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });

function cents(value) {
  return `${Number(value || 0).toFixed(1)}\u00a2`;
}

function pct(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function relTime(timestamp) {
  if (!timestamp) return "just now";
  const delta = Math.max(0, Date.now() / 1000 - timestamp);
  if (delta < 10) return "just now";
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  return `${Math.round(delta / 3600)}h ago`;
}

function titleCase(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function platformLabel(platform) {
  const labels = {
    kalshi: "Kalshi",
    polymarket: "Polymarket",
    predictit: "PredictIt",
  };
  return labels[platform] || titleCase(platform);
}

function statusClass(status) {
  if (["tradable", "healthy", "closed", "filled", "submitted", "simulated", "live", "ok", "resolved", "manual_closed", "confirmed"].includes(status)) {
    return "badge badge-tradable";
  }
  if (["manual", "manual_pending", "awaiting-entry", "entered", "manual_entered"].includes(status)) {
    return "badge badge-manual";
  }
  if (["recovering", "failed", "critical", "open", "cancelled", "manual_cancelled"].includes(status)) {
    return "badge badge-critical";
  }
  return "badge badge-review";
}

function circuitStateLabel(stateValue) {
  return String(stateValue || "unknown").replace(/_/g, " ").toLowerCase();
}

function collectorCircuitState(collector) {
  const states = [
    collector?.circuit?.state,
    collector?.gamma_circuit?.state,
    collector?.clob_circuit?.state,
  ]
    .filter(Boolean)
    .map(circuitStateLabel);

  if (!states.length) return "unknown";
  if (states.includes("open")) return "open";
  if (states.includes("half open")) return "recovering";
  if (states.includes("closed")) return "closed";
  return states[0];
}

function opportunityTone(status) {
  if (status === "manual") return "tone-plum";
  if (status === "tradable") return "tone-mint";
  if (status === "review" || status === "candidate") return "tone-amber";
  if (status === "stale" || status === "illiquid") return "tone-blue";
  return "tone-slate";
}

function executionTone(status) {
  if (status === "manual_pending" || status === "manual_entered") return "tone-plum";
  if (status === "manual_closed") return "tone-mint";
  if (status === "manual_cancelled") return "tone-rose";
  if (status === "failed" || status === "recovering") return "tone-rose";
  if (status === "submitted" || status === "filled" || status === "simulated") return "tone-gold";
  return "tone-slate";
}

function balanceTone(snapshot) {
  return snapshot?.is_low ? "tone-amber" : "tone-blue";
}

function collectorTone(collector) {
  const circuitState = collectorCircuitState(collector);
  if (circuitState === "open") return "tone-rose";
  if ((collector?.consecutive_errors || 0) > 0 || (collector?.total_errors || 0) > 0) return "tone-amber";
  return "tone-blue";
}

function mappingTone(status) {
  if (status === "confirmed") return "tone-mint";
  if (status === "disabled") return "tone-rose";
  if (status === "review") return "tone-amber";
  return "tone-slate";
}

function mappingStatus(mapping) {
  return String(mapping?.status || "candidate");
}

function fetchTrackedCount(key) {
  if (key === "opportunities") return state.opportunities.length;
  if (key === "trades") return state.trades.length;
  if (key === "manualPositions") return state.manualPositions.length;
  if (key === "incidents") return state.incidents.length;
  if (key === "balances") return Object.keys(state.system?.balances || {}).length;
  if (key === "collectors") return Object.keys(state.system?.collectors || {}).length;
  if (key === "mappings") return state.mappings.length;
  return 0;
}

function fetchPublishedCount(key) {
  if (key === "system") return state.system ? 1 : 0;
  if (key === "quote") return state.system?.counts?.prices || 0;
  if (key === "opportunity") return state.opportunities.length;
  if (key === "execution") return state.trades.length;
  if (key === "incident") return state.incidents.length;
  if (key === "heartbeat") return state.lastQuoteAt ? 1 : 0;
  return 0;
}

function summarizeIncidentMetadata(metadata) {
  if (!metadata || !Object.keys(metadata).length) {
    return "Recovery context is available in the execution engine if the condition persists.";
  }
  if (metadata.original_yes != null && metadata.current_yes != null && metadata.original_no != null && metadata.current_no != null) {
    return `YES moved from ${formatUsd.format(metadata.original_yes)} to ${formatUsd.format(metadata.current_yes)} while NO moved from ${formatUsd.format(metadata.original_no)} to ${formatUsd.format(metadata.current_no)}.`;
  }
  if (metadata.leg_yes || metadata.leg_no) {
    const yesSummary = metadata.leg_yes
      ? `${platformLabel(metadata.leg_yes.platform)} YES ${titleCase(metadata.leg_yes.status)}`
      : "YES leg unavailable";
    const noSummary = metadata.leg_no
      ? `${platformLabel(metadata.leg_no.platform)} NO ${titleCase(metadata.leg_no.status)}`
      : "NO leg unavailable";
    return `${yesSummary}; ${noSummary}.`;
  }
  return Object.entries(metadata)
    .slice(0, 3)
    .map(([key, value]) => `${titleCase(key)} ${typeof value === "number" ? Number(value).toFixed(2) : String(value)}`)
    .join(" • ");
}

function opportunityReadiness(opp) {
  if (opp.status === "tradable") return "Auto-trade ready after persistence, liquidity, and freshness gates passed.";
  if (opp.status === "manual") return "A manual venue is in the route, so Arbiter is holding for operator confirmation.";
  if (opp.status === "review") return "Mapping or auto-trade policy needs review before Arbiter can act.";
  if (opp.status === "stale") return "Quotes aged out before the route was safe to publish.";
  if (opp.status === "illiquid") return "Liquidity is thinner than the configured minimum.";
  return "The route is still building persistence and confidence.";
}

function renderLogTokens(items, getCount) {
  return items
    .map((item) => {
      const count = getCount(item.key);
      const detail = count ? `${formatWhole.format(count)} ${item.detail || "tracked"}` : item.detail || "idle";
      return `
        <div class="log-token">
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(detail)}</strong>
        </div>
      `;
    })
    .join("");
}

function buildLogCategoryCounts() {
  const collectors = state.system?.collectors || {};
  const balances = state.system?.balances || {};
  return {
    market: state.system ? 1 : 0,
    opportunity: state.opportunities.length,
    execution: state.trades.filter((trade) => !String(trade.status || "").startsWith("manual_")).length,
    manual: state.manualPositions.length + state.trades.filter((trade) => String(trade.status || "").startsWith("manual_")).length,
    incident: state.incidents.length,
    balance: Object.keys(balances).length,
    collector: Object.keys(collectors).length,
    mapping: state.mappings.length,
  };
}

function buildMarketEntry() {
  if (!state.system) return null;
  const trackedMarketCount = Object.keys(state.system.tracked_markets || {}).length;
  const published = state.system.scanner?.published || 0;
  const pulseTimestamp = state.lastQuoteAt || state.system.timestamp;
  return {
    id: "market-pulse",
    category: "market",
    tone: "tone-mint",
    title: "Live market pulse",
    headline: `${formatWhole.format(state.system.counts?.prices || 0)} quotes across ${formatWhole.format(trackedMarketCount)} tracked markets`,
    narrative: state.lastQuoteAt
      ? `WebSocket ${state.wsConnected ? "is live" : "is reconnecting"}, the latest quote landed ${relTime(state.lastQuoteAt)}, and the scanner has already published ${formatWhole.format(published)} qualified opportunities.`
      : "The dashboard is waiting for the first quote heartbeat from the collectors.",
    tags: [
      `Mode ${state.system.mode === "live" ? "Live" : "Dry Run"}`,
      `Watchlist ${formatWhole.format(trackedMarketCount)}`,
      `Published ${formatWhole.format(published)}`,
    ],
    footnote: "Published live",
    timestamp: pulseTimestamp || Date.now() / 1000,
    synthetic: false,
    rank: 0,
  };
}

function buildOpportunityEntry(opp, index) {
  return {
    id: `opportunity-${opp.canonical_id}-${opp.yes_platform}-${opp.no_platform}`,
    category: "opportunity",
    tone: opportunityTone(opp.status),
    title: opp.description || opp.canonical_id,
    headline: `${titleCase(opp.status)} opportunity at ${cents(opp.net_edge_cents)}`,
    narrative: `${opportunityReadiness(opp)} ${platformLabel(opp.yes_platform)} YES ${formatUsd.format(opp.yes_price || 0)} and ${platformLabel(opp.no_platform)} NO ${formatUsd.format(opp.no_price || 0)} imply gross ${cents((opp.gross_edge || 0) * 100)}, fees ${cents((opp.total_fees || 0) * 100)}, and max ${formatUsd.format(opp.max_profit_usd || 0)}.`,
    tags: [
      `Confidence ${pct(opp.confidence || 0)}`,
      `Qty ${formatWhole.format(opp.suggested_qty || 0)}`,
      `${opp.persistence_count || 0}/${state.system?.scanner?.persistence_scans || 0} scans`,
      `Age ${(opp.quote_age_seconds || 0).toFixed(1)}s`,
    ],
    footnote: opp.requires_manual ? "Published live + tracked" : "Tracked",
    timestamp: opp.timestamp || 0,
    synthetic: false,
    rank: index,
  };
}

function buildTradeEntry(trade, index) {
  const manual = String(trade.status || "").startsWith("manual_");
  const yesPlatform = platformLabel(trade.leg_yes?.platform);
  const noPlatform = platformLabel(trade.leg_no?.platform);
  const quantity = Math.max(trade.leg_yes?.quantity || 0, trade.leg_no?.quantity || 0);
  return {
    id: `trade-${trade.arb_id}`,
    category: manual ? "manual" : "execution",
    tone: executionTone(trade.status),
    title: trade.opportunity?.description || trade.opportunity?.canonical_id || trade.arb_id,
    headline: `${titleCase(trade.status)} ${manual ? "workflow" : "execution"} for ${trade.arb_id}`,
    narrative: `${yesPlatform} YES ${formatUsd.format(trade.leg_yes?.price || 0)} and ${noPlatform} NO ${formatUsd.format(trade.leg_no?.price || 0)} across ${formatWhole.format(quantity)} contracts. ${manual ? "Arbiter staged the trade for manual handling." : `Realized P&L is ${formatUsd.format(trade.realized_pnl || 0)}.`}${trade.notes?.length ? ` Notes: ${trade.notes.join(", ")}.` : ""}`,
    tags: [
      `Status ${titleCase(trade.status)}`,
      `P&L ${formatUsd.format(trade.realized_pnl || 0)}`,
      `Qty ${formatWhole.format(quantity)}`,
      trade.arb_id,
    ],
    footnote: manual ? "Tracked" : "Published live + tracked",
    timestamp: trade.timestamp || 0,
    synthetic: false,
    rank: index,
  };
}

function buildManualEntry(position, index) {
  return {
    id: `manual-${position.position_id}`,
    category: "manual",
    tone: "tone-plum",
    title: position.description || position.position_id,
    headline: `Manual queue opened on ${platformLabel(position.yes_platform)} and ${platformLabel(position.no_platform)}`,
    narrative: position.instructions || "Manual execution instructions are ready for operator review.",
    tags: [
      `Status ${titleCase(position.status)}`,
      `Qty ${formatWhole.format(position.quantity || 0)}`,
      `${platformLabel(position.yes_platform)} YES`,
      `${platformLabel(position.no_platform)} NO`,
    ],
    footnote: "Tracked",
    timestamp: position.timestamp || 0,
    synthetic: false,
    rank: index,
  };
}

function buildIncidentEntry(incident, index) {
  const incidentStatus = incident.status || "open";
  return {
    id: `incident-${incident.incident_id}`,
    category: "incident",
    tone: incidentStatus === "resolved" ? "tone-blue" : (incident.severity === "critical" ? "tone-rose" : "tone-amber"),
    title: incident.message || incident.incident_id,
    headline: `${titleCase(incidentStatus)} ${titleCase(incident.severity)} incident on ${incident.canonical_id || "execution flow"}`,
    narrative: `${summarizeIncidentMetadata(incident.metadata)}${incident.resolution_note ? ` Resolution: ${incident.resolution_note}.` : ""}`,
    tags: [
      `Status ${titleCase(incidentStatus)}`,
      `Severity ${titleCase(incident.severity)}`,
      incident.arb_id || "No arb id",
      incident.canonical_id || "No market id",
    ],
    footnote: "Published live + tracked",
    timestamp: incident.timestamp || 0,
    synthetic: false,
    rank: index,
  };
}

function buildBalanceEntry(platform, snapshot, index) {
  const timestamp = snapshot?.timestamp || state.system?.timestamp || Date.now() / 1000;
  return {
    id: `balance-${platform}`,
    category: "balance",
    tone: balanceTone(snapshot),
    title: `${platformLabel(platform)} ${snapshot?.is_low ? "needs funding" : "is funded"}`,
    headline: `${formatUsd.format(snapshot?.balance || 0)} available on ${platformLabel(platform)}`,
    narrative: snapshot?.is_low
      ? "The venue is below its configured threshold and may block new opportunities until it is funded."
      : "Funding is above the configured threshold and the venue can continue supporting new routes.",
    tags: [
      snapshot?.is_low ? "Low balance" : "Healthy",
      `Updated ${relTime(timestamp)}`,
      platformLabel(platform),
    ],
    footnote: "Tracked",
    timestamp,
    synthetic: true,
    rank: index,
  };
}

function buildCollectorEntry(name, collector, index) {
  const circuitState = collectorCircuitState(collector);
  const timestamp = state.system?.timestamp || Date.now() / 1000;
  return {
    id: `collector-${name}`,
    category: "collector",
    tone: collectorTone(collector),
    title: `${platformLabel(name)} collector ${circuitState === "open" ? "is throttled" : circuitState === "recovering" ? "is recovering" : "is stable"}`,
    headline: `${formatWhole.format(collector?.total_fetches || 0)} fetches with ${formatWhole.format(collector?.total_errors || 0)} errors`,
    narrative:
      circuitState === "open"
        ? "The circuit breaker is open, so Arbiter is intentionally backing off until the venue stabilizes."
        : circuitState === "recovering"
          ? "The collector is probing the venue again after prior instability."
          : "The collector is polling normally and feeding the dashboard with fresh market state.",
    tags: [
      `Circuit ${titleCase(circuitState)}`,
      `Consecutive ${formatWhole.format(collector?.consecutive_errors || 0)}`,
      platformLabel(name),
    ],
    footnote: "Tracked",
    timestamp: timestamp - (index * 0.001),
    synthetic: true,
    rank: index,
  };
}

function buildMappingEntry(mapping, index) {
  const status = mappingStatus(mapping);
  const platforms = [mapping.kalshi, mapping.polymarket, mapping.predictit]
    .filter(Boolean)
    .map(String)
    .join(" • ");
  const timestamp = state.system?.timestamp || Date.now() / 1000;
  return {
    id: `mapping-${mapping.canonical_id}`,
    category: "mapping",
    tone: mappingTone(status),
    title: mapping.description || mapping.canonical_id,
    headline: `${titleCase(status)} mapping for cross-venue trade identity`,
    narrative: `${platforms || "No venue ids registered yet."} ${mapping.notes || "Only confirmed mappings are eligible for dependable auto-trading."}`,
    tags: [
      `Status ${titleCase(status)}`,
      mapping.allow_auto_trade ? "Auto-trade allowed" : "Review before auto-trade",
      mapping.canonical_id,
    ],
    footnote: "Tracked",
    timestamp: timestamp - (index * 0.002),
    synthetic: true,
    rank: index,
  };
}

function buildLogEntries() {
  const entries = [];
  const marketEntry = buildMarketEntry();
  if (marketEntry) entries.push(marketEntry);

  state.opportunities.slice(0, 12).forEach((opp, index) => entries.push(buildOpportunityEntry(opp, index)));
  state.trades.slice(0, 10).forEach((trade, index) => entries.push(buildTradeEntry(trade, index)));
  state.manualPositions.slice(0, 8).forEach((position, index) => entries.push(buildManualEntry(position, index)));
  state.incidents.slice(0, 10).forEach((incident, index) => entries.push(buildIncidentEntry(incident, index)));

  Object.entries(state.system?.balances || {}).forEach(([platform, snapshot], index) => {
    entries.push(buildBalanceEntry(platform, snapshot, index));
  });

  Object.entries(state.system?.collectors || {}).forEach(([name, collector], index) => {
    entries.push(buildCollectorEntry(name, collector, index));
  });

  const nonConfirmedMappings = state.mappings.filter((mapping) => mappingStatus(mapping) !== "confirmed");
  const visibleMappings = nonConfirmedMappings.length ? nonConfirmedMappings.slice(0, 6) : state.mappings.slice(0, 3);
  visibleMappings.forEach((mapping, index) => entries.push(buildMappingEntry(mapping, index)));

  return entries
    .sort((left, right) => {
      if (left.synthetic !== right.synthetic) return left.synthetic ? 1 : -1;
      if (right.timestamp !== left.timestamp) return right.timestamp - left.timestamp;
      return left.rank - right.rank;
    })
    .slice(0, 32);
}

function renderLogEntry(entry) {
  const category = LOG_DEFINITIONS[entry.category];
  return `
    <article class="log-entry ${entry.tone}">
      <div class="log-entry-head">
        <div class="log-entry-kicker">
          <span class="log-entry-source">${escapeHtml(entry.footnote)}</span>
          <span class="log-entry-dot"></span>
          <span>${escapeHtml(category.label)}</span>
        </div>
        <div class="log-entry-time">${escapeHtml(relTime(entry.timestamp))}</div>
      </div>
      <div class="log-entry-body">
        <div>
          <h3>${escapeHtml(entry.title)}</h3>
          <p class="log-entry-headline">${escapeHtml(entry.headline)}</p>
        </div>
        <span class="log-entry-badge">${escapeHtml(category.label)}</span>
      </div>
      <p class="log-entry-narrative">${escapeHtml(entry.narrative)}</p>
      <div class="log-entry-tags">
        ${entry.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}
      </div>
    </article>
  `;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} failed with ${response.status}`);
  return response.json();
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(`${path} failed with ${response.status}: ${message}`);
  }
  return response.json();
}

async function loadSnapshot() {
  const [system, opportunities, trades, incidents, manualPositions, mappings] = await Promise.all([
    fetchJson("/api/system"),
    fetchJson("/api/opportunities"),
    fetchJson("/api/trades"),
    fetchJson("/api/errors"),
    fetchJson("/api/manual-positions"),
    fetchJson("/api/market-mappings"),
  ]);
  state.system = system;
  state.opportunities = opportunities;
  state.trades = trades;
  state.incidents = incidents;
  state.manualPositions = manualPositions;
  state.mappings = mappings;
  render();
}

function upsertOpportunity(opportunity) {
  const key = `${opportunity.canonical_id}:${opportunity.yes_platform}:${opportunity.no_platform}`;
  const existingIndex = state.opportunities.findIndex((item) => `${item.canonical_id}:${item.yes_platform}:${item.no_platform}` === key);
  if (existingIndex >= 0) {
    state.opportunities[existingIndex] = opportunity;
  } else {
    state.opportunities.unshift(opportunity);
  }
  state.opportunities = state.opportunities
    .sort((left, right) => (right.net_edge_cents || 0) - (left.net_edge_cents || 0))
    .slice(0, 16);
}

function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);

  socket.addEventListener("open", () => {
    state.wsConnected = true;
    wsStatusEl.textContent = "Live";
    wsStatusEl.classList.remove("value-muted");
    socket.send(JSON.stringify({ action: "refresh" }));
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "bootstrap" || message.type === "system") {
      state.system = message.payload;
    } else if (message.type === "quote") {
      state.lastQuoteAt = message.payload.timestamp;
      if (state.system?.counts) {
        state.system.counts.prices = Math.max(state.system.counts.prices || 0, 1);
      }
    } else if (message.type === "opportunity") {
      upsertOpportunity(message.payload);
    } else if (message.type === "execution") {
      state.trades.unshift(message.payload);
      state.trades = state.trades.slice(0, 24);
      if (state.system?.series?.equity) {
        const latest = state.system.series.equity[state.system.series.equity.length - 1]?.equity || 0;
        state.system.series.equity.push({
          timestamp: message.payload.timestamp,
          equity: latest + (message.payload.realized_pnl || 0),
        });
        state.system.series.equity = state.system.series.equity.slice(-180);
      }
    } else if (message.type === "incident") {
      state.incidents.unshift(message.payload);
      state.incidents = state.incidents.slice(0, 24);
    } else if (message.type === "heartbeat") {
      state.lastQuoteAt = message.payload.timestamp;
    }
    render();
  });

  socket.addEventListener("close", () => {
    state.wsConnected = false;
    wsStatusEl.textContent = "Reconnecting";
    wsStatusEl.classList.add("value-muted");
    window.setTimeout(connectWebSocket, 1500);
  });

  socket.addEventListener("error", () => socket.close());
}

function render() {
  renderMetrics();
  renderOpportunities();
  renderManualQueue();
  renderIncidentQueue();
  renderLogExperience();
  renderMappings();
  renderCollectors();
  renderCharts();
}

function renderMetrics() {
  const metrics = document.getElementById("metricGrid");
  const system = state.system;
  if (!metrics || !system) return;

  modePillEl.textContent = system.mode === "live" ? "Live" : "Dry Run";

  const cards = [
    {
      label: "Tradable opportunities",
      value: formatWhole.format(system.scanner?.tradable_opportunities || 0),
      meta: `${formatWhole.format(system.scanner?.active_opportunities || 0)} active across the watchlist`,
    },
    {
      label: "Best live edge",
      value: cents(system.scanner?.best_edge_cents || 0),
      meta: `Persistence gate: ${formatWhole.format(system.scanner?.persistence_scans || 0)} scans`,
    },
    {
      label: "Realized P&L",
      value: formatUsd.format(system.execution?.total_pnl || 0),
      meta: `${formatWhole.format(system.execution?.total_executions || 0)} tracked executions`,
    },
    {
      label: "Collector quotes",
      value: formatWhole.format(system.counts?.prices || 0),
      meta: state.lastQuoteAt ? `Last quote ${relTime(state.lastQuoteAt)}` : "Waiting for quotes",
    },
    {
      label: "Runtime uptime",
      value: `${Math.floor((system.uptime_seconds || 0) / 60)}m`,
      meta: `${formatWhole.format(system.counts?.incidents || 0)} incidents, ${formatWhole.format(system.counts?.manual_positions || 0)} manual positions`,
    },
  ];

  metrics.innerHTML = cards
    .map((card) => `
      <article class="metric-card">
        <div class="metric-label">${escapeHtml(card.label)}</div>
        <div class="metric-value">${escapeHtml(card.value)}</div>
        <div class="metric-meta">${escapeHtml(card.meta)}</div>
      </article>
    `)
    .join("");

  document.getElementById("bestEdgeLabel").textContent = cents(system.scanner?.best_edge_cents || 0);
  document.getElementById("equityLabel").textContent = formatUsd.format(system.execution?.total_pnl || 0);
}

function renderOpportunities() {
  const container = document.getElementById("opportunityList");
  document.getElementById("opportunityCount").textContent = formatWhole.format(state.opportunities.length);
  if (!container) return;
  if (!state.opportunities.length) {
    container.innerHTML = emptyState("No fee-positive opportunities are active right now.");
    return;
  }

  container.innerHTML = state.opportunities.slice(0, 10).map((opp) => {
    const gross = Math.max(opp.gross_edge || 0, 0.0001);
    const fees = Math.max(opp.total_fees || 0, 0.0001);
    const net = Math.max(opp.net_edge || 0, 0.0001);
    return `
      <article class="opportunity-card">
        <div class="opportunity-top">
          <div>
            <div class="opportunity-title">${escapeHtml(opp.description)}</div>
            <div class="opportunity-meta">
              <span class="${statusClass(opp.status)}">${escapeHtml(opp.status)}</span>
              <span class="badge badge-review">${escapeHtml(`${opp.persistence_count || 0}/${state.system?.scanner?.persistence_scans || 3} scans`)}</span>
              <span class="badge badge-review">${escapeHtml(relTime(opp.timestamp))}</span>
              <span class="badge badge-review">${escapeHtml(`Freshness ${pct(Math.max(0, 1 - (opp.quote_age_seconds || 0) / (state.system?.scanner?.max_quote_age_seconds || 15)))}`)}</span>
            </div>
          </div>
          <div class="panel-badge">${escapeHtml(cents(opp.net_edge_cents))}</div>
        </div>
        <div class="opportunity-legs">
          ${renderLegCard("Buy YES", opp.yes_platform, opp.yes_price, opp.yes_fee, opp.yes_market_id)}
          ${renderLegCard("Buy NO", opp.no_platform, opp.no_price, opp.no_fee, opp.no_market_id)}
        </div>
        <div class="waterfall">
          <div class="waterfall-track" style="--gross:${gross}; --fees:${fees}; --net:${net};">
            <div class="segment-gross"></div>
            <div class="segment-fees"></div>
            <div class="segment-net"></div>
          </div>
          <div class="waterfall-labels">
            <span>${escapeHtml(`Gross ${cents((opp.gross_edge || 0) * 100)}`)}</span>
            <span>${escapeHtml(`Fees ${cents((opp.total_fees || 0) * 100)}`)}</span>
            <span>${escapeHtml(`Net ${cents(opp.net_edge_cents || 0)}`)}</span>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

function renderManualQueue() {
  const container = document.getElementById("manualQueue");
  const countEl = document.getElementById("manualCount");
  if (countEl) countEl.textContent = formatWhole.format(state.manualPositions.length);
  if (!container) return;
  if (!state.manualPositions.length) {
    container.innerHTML = emptyState("No manual venues need operator action right now.");
    return;
  }

  container.innerHTML = state.manualPositions.slice(0, 6).map((position) => {
    const buttons = [];
    if (position.status === "awaiting-entry") {
      buttons.push(renderActionButton("Mark entered", "mark_entered", "manual", position.position_id, position.canonical_id));
      buttons.push(renderActionButton("Cancel", "cancel", "manual", position.position_id, position.canonical_id, true));
    } else if (position.status === "entered") {
      buttons.push(renderActionButton("Mark closed", "mark_closed", "manual", position.position_id, position.canonical_id));
    }

    return `
      <article class="stack-item tone-plum operator-card" data-manual-id="${escapeHtml(position.position_id)}" data-manual-canonical="${escapeHtml(position.canonical_id)}">
        <div class="stack-item-header">
          <div class="stack-item-title">${escapeHtml(position.description || position.position_id)}</div>
          <span class="${statusClass(position.status)}" data-manual-status>${escapeHtml(titleCase(position.status))}</span>
        </div>
        <div class="mapping-platforms">
          <span>${escapeHtml(`${platformLabel(position.yes_platform)} YES ${formatUsd.format(position.yes_price || 0)}`)}</span>
          <span>${escapeHtml(`${platformLabel(position.no_platform)} NO ${formatUsd.format(position.no_price || 0)}`)}</span>
          <span>${escapeHtml(`Qty ${formatWhole.format(position.quantity || 0)}`)}</span>
        </div>
        <p class="stack-item-meta">${escapeHtml(position.instructions || "Manual execution instructions are ready.")}</p>
        <div class="operator-meta-row">
          <span>${escapeHtml(`Opened ${relTime(position.timestamp)}`)}</span>
          <span>${escapeHtml(position.note || "Awaiting operator acknowledgement.")}</span>
        </div>
        ${buttons.length ? `<div class="action-row">${buttons.join("")}</div>` : ""}
      </article>
    `;
  }).join("");
}

function renderIncidentQueue() {
  const container = document.getElementById("incidentList");
  const countEl = document.getElementById("incidentCount");
  if (countEl) countEl.textContent = formatWhole.format(state.incidents.length);
  if (!container) return;
  if (!state.incidents.length) {
    container.innerHTML = emptyState("No recovery incidents are open.");
    return;
  }

  container.innerHTML = state.incidents.slice(0, 6).map((incident) => `
    <article class="stack-item ${incident.status === "resolved" ? "tone-blue" : "tone-rose"} operator-card" data-incident-id="${escapeHtml(incident.incident_id)}">
      <div class="stack-item-header">
        <div class="stack-item-title">${escapeHtml(incident.message || incident.incident_id)}</div>
        <span class="${statusClass(incident.status || "open")}" data-incident-status>${escapeHtml(titleCase(incident.status || "open"))}</span>
      </div>
      <div class="mapping-platforms">
        <span>${escapeHtml(`Severity ${titleCase(incident.severity)}`)}</span>
        <span>${escapeHtml(incident.canonical_id || "Execution flow")}</span>
        <span>${escapeHtml(relTime(incident.timestamp))}</span>
      </div>
      <p class="stack-item-meta">${escapeHtml(summarizeIncidentMetadata(incident.metadata))}</p>
      <div class="operator-meta-row">
        <span>${escapeHtml(incident.resolution_note || "Still waiting for operator resolution.")}</span>
      </div>
      ${incident.status !== "resolved" ? `<div class="action-row">${renderActionButton("Resolve", "resolve", "incident", incident.incident_id, incident.canonical_id || "")}</div>` : ""}
    </article>
  `).join("");
}

function renderLegCard(label, platform, price, fee, marketId) {
  const normalizedPlatform = String(platform || "unknown").toLowerCase();
  return `
    <div class="leg-card">
      <div class="leg-label">${escapeHtml(label)}</div>
      <div class="leg-platform">
        <span class="platform-chip platform-${escapeHtml(normalizedPlatform)}">${escapeHtml(platformLabel(platform))}</span>
        <span class="leg-subtext">${escapeHtml(`Fee ${cents((fee || 0) * 100)}`)}</span>
      </div>
      <div class="leg-price">${escapeHtml(formatUsd.format(price || 0))}</div>
      <div class="leg-subtext">${escapeHtml(marketId || "Unmapped leg")}</div>
    </div>
  `;
}

function renderLogExperience() {
  const categoryCounts = buildLogCategoryCounts();
  const entries = buildLogEntries();
  const filteredEntries = state.activeLogFilter === "all"
    ? entries
    : entries.filter((entry) => entry.category === state.activeLogFilter);

  const publishedTypes = document.getElementById("publishedTypes");
  const trackedTypes = document.getElementById("trackedTypes");
  const categoryAtlas = document.getElementById("logCategoryAtlas");
  const filterBar = document.getElementById("logFilterBar");
  const timeline = document.getElementById("logTimeline");
  const visibleCount = document.getElementById("logVisibleCount");
  const freshness = document.getElementById("logFreshnessLabel");
  const categoryCount = document.getElementById("logCategoryCount");

  if (publishedTypes) {
    publishedTypes.innerHTML = renderLogTokens(PUBLISHED_TYPES, fetchPublishedCount);
  }
  if (trackedTypes) {
    trackedTypes.innerHTML = renderLogTokens(TRACKED_TYPES, fetchTrackedCount);
  }

  const activeCategories = Object.values(categoryCounts).filter((count) => count > 0).length;
  if (categoryCount) {
    categoryCount.textContent = formatWhole.format(activeCategories);
  }

  if (categoryAtlas) {
    categoryAtlas.innerHTML = FILTER_ORDER.filter((key) => key !== "all").map((key) => {
      const definition = LOG_DEFINITIONS[key];
      const count = categoryCounts[key] || 0;
      return `
        <button type="button" class="log-category-card ${state.activeLogFilter === key ? "is-active" : ""}" data-log-filter="${escapeHtml(key)}">
          <div class="log-category-top">
            <span class="log-category-name">${escapeHtml(definition.label)}</span>
            <span class="log-category-count">${escapeHtml(formatWhole.format(count))}</span>
          </div>
          <p>${escapeHtml(definition.description)}</p>
          <div class="log-category-source">${escapeHtml(definition.source)}</div>
        </button>
      `;
    }).join("");
  }

  if (filterBar) {
    filterBar.innerHTML = FILTER_ORDER.filter((key) => key === "all" || (categoryCounts[key] || 0) > 0).map((key) => {
      const count = key === "all" ? entries.length : categoryCounts[key];
      const label = key === "all" ? "All activity" : LOG_DEFINITIONS[key].label;
      return `
        <button type="button" class="log-filter-chip ${state.activeLogFilter === key ? "is-active" : ""}" data-log-filter="${escapeHtml(key)}">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(formatWhole.format(count || 0))}</strong>
        </button>
      `;
    }).join("");
  }

  if (visibleCount) {
    visibleCount.textContent = `${formatWhole.format(filteredEntries.length)} shown`;
  }
  if (freshness) {
    freshness.textContent = state.lastQuoteAt ? relTime(state.lastQuoteAt) : (state.system ? relTime(state.system.timestamp) : "Waiting");
    freshness.classList.toggle("value-muted", !state.lastQuoteAt);
  }

  if (!timeline) return;
  if (!filteredEntries.length) {
    timeline.innerHTML = emptyState("No events match this category right now.");
    return;
  }

  timeline.innerHTML = filteredEntries.map(renderLogEntry).join("");
}

function renderMappings() {
  const container = document.getElementById("mappingList");
  document.getElementById("mappingCount").textContent = formatWhole.format(state.mappings.length);
  if (!container) return;
  if (!state.mappings.length) {
    container.innerHTML = emptyState("No canonical mappings are loaded.");
    return;
  }

  container.innerHTML = state.mappings.slice(0, 8).map((mapping) => `
    <article class="stack-item ${mappingTone(mappingStatus(mapping))} operator-card" data-mapping-id="${escapeHtml(mapping.canonical_id)}">
      <div class="stack-item-header">
        <div class="stack-item-title">${escapeHtml(mapping.description || mapping.canonical_id)}</div>
        <span class="${statusClass(mappingStatus(mapping))}" data-mapping-status>${escapeHtml(mappingStatus(mapping))}</span>
      </div>
      <div class="mapping-platforms">
        ${mapping.kalshi ? `<span>${escapeHtml(`Kalshi ${mapping.kalshi}`)}</span>` : ""}
        ${mapping.polymarket ? `<span>${escapeHtml(`Polymarket ${mapping.polymarket}`)}</span>` : ""}
        ${mapping.predictit ? `<span>${escapeHtml(`PredictIt ${mapping.predictit}`)}</span>` : ""}
      </div>
      <div class="stack-item-meta">${escapeHtml(mapping.review_note || mapping.notes || "Confirmed mappings are the only auto-tradable candidates.")}</div>
      <div class="operator-meta-row">
        <span>${escapeHtml(mapping.allow_auto_trade ? "Auto-trade allowed" : "Held for review before auto-trade")}</span>
      </div>
      <div class="action-row">
        ${mappingStatus(mapping) !== "confirmed" ? renderActionButton("Confirm match", "confirm", "mapping", mapping.canonical_id, mapping.canonical_id) : ""}
        ${mappingStatus(mapping) !== "review" ? renderActionButton("Mark review", "review", "mapping", mapping.canonical_id, mapping.canonical_id, true) : ""}
      </div>
    </article>
  `).join("");
}

function renderCollectors() {
  const container = document.getElementById("collectorList");
  const collectors = state.system?.collectors || {};
  document.getElementById("collectorCount").textContent = formatWhole.format(Object.keys(collectors).length);
  if (!container) return;
  if (!Object.keys(collectors).length) {
    container.innerHTML = emptyState("Collector health will appear after the backend starts streaming.");
    return;
  }

  container.innerHTML = Object.entries(collectors).map(([name, collector]) => {
    const circuitState = collectorCircuitState(collector);
    const collectorStatus = circuitState === "open"
      ? "critical"
      : (collector.total_errors || 0) > 0 || (collector.consecutive_errors || 0) > 0
        ? "review"
        : "tradable";
    return `
      <article class="stack-item">
        <div class="stack-item-header">
          <div class="stack-item-title">${escapeHtml(platformLabel(name))}</div>
          <span class="${statusClass(collectorStatus)}">${escapeHtml(`${collector.total_errors || 0} errors`)}</span>
        </div>
        <div class="stack-item-meta">
          ${escapeHtml(`Fetches ${formatWhole.format(collector.total_fetches || 0)} • Consecutive ${formatWhole.format(collector.consecutive_errors || 0)}`)}<br>
          ${escapeHtml(`Circuit ${titleCase(circuitState)}`)}
        </div>
      </article>
    `;
  }).join("");
}

function renderCharts() {
  const scannerSeries = state.system?.series?.scanner || [];
  const equitySeries = state.system?.series?.equity || [];
  drawLineChart(edgeChartEl, scannerSeries, {
    valueKey: "best_edge_cents",
    color: "#8ce0cf",
    fill: "rgba(140, 224, 207, 0.28)",
    axisSuffix: "\u00a2",
  });
  drawLineChart(equityChartEl, equitySeries, {
    valueKey: "equity",
    color: "#ffd178",
    fill: "rgba(255, 209, 120, 0.22)",
    axisPrefix: "$",
  });
}

function drawLineChart(target, points, options) {
  if (!target) return;
  const width = target.clientWidth;
  const height = Math.max(target.clientHeight || 260, 220);
  if (!width) {
    window.requestAnimationFrame(() => drawLineChart(target, points, options));
    return;
  }

  if (!points.length) {
    target.innerHTML = emptyState("Waiting for live series data.");
    return;
  }

  const padding = { top: 16, right: 20, bottom: 24, left: 16 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  const values = points.map((point) => Number(point[options.valueKey] || 0));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1e-6);

  const coords = points.map((point, index) => {
    const x = padding.left + (innerWidth * (points.length === 1 ? 0 : index / (points.length - 1)));
    const y = padding.top + innerHeight - ((Number(point[options.valueKey] || 0) - min) / range) * innerHeight;
    return [x, y];
  });

  const linePath = coords.map(([x, y], index) => `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
  const fillPath = `${linePath} L ${padding.left + innerWidth} ${padding.top + innerHeight} L ${padding.left} ${padding.top + innerHeight} Z`;
  const gridLines = [0, 0.5, 1].map((fraction) => {
    const y = padding.top + innerHeight * fraction;
    return `<line class="chart-grid-line" x1="${padding.left}" y1="${y}" x2="${padding.left + innerWidth}" y2="${y}"></line>`;
  }).join("");
  const minLabel = `${options.axisPrefix || ""}${min.toFixed(1)}${options.axisSuffix || ""}`;
  const maxLabel = `${options.axisPrefix || ""}${max.toFixed(1)}${options.axisSuffix || ""}`;
  const lastLabel = formatClock.format(new Date((points[points.length - 1]?.timestamp || Date.now() / 1000) * 1000));

  target.innerHTML = `
    <svg class="chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Live chart">
      <defs>
        <linearGradient id="fill-${target.id}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${options.fill}"></stop>
          <stop offset="100%" stop-color="rgba(0,0,0,0)"></stop>
        </linearGradient>
      </defs>
      ${gridLines}
      <path class="chart-fill" d="${fillPath}" fill="url(#fill-${target.id})"></path>
      <path class="chart-line" d="${linePath}" stroke="${options.color}"></path>
      <text class="chart-axis-label" x="${padding.left}" y="${padding.top + 12}">${escapeHtml(maxLabel)}</text>
      <text class="chart-axis-label" x="${padding.left}" y="${padding.top + innerHeight - 6}">${escapeHtml(minLabel)}</text>
      <text class="chart-axis-label" x="${padding.left + innerWidth - 36}" y="${height - 8}">${escapeHtml(lastLabel)}</text>
    </svg>
  `;
}

function emptyState(message) {
  return `<article class="stack-item"><div class="stack-item-meta">${escapeHtml(message)}</div></article>`;
}

function renderActionButton(label, action, scope, targetId, canonicalId, secondary = false) {
  return `
    <button
      type="button"
      class="action-button ${secondary ? "action-button-secondary" : ""}"
      data-${scope}-action="${escapeHtml(action)}"
      data-target-id="${escapeHtml(targetId)}"
      data-canonical-id="${escapeHtml(canonicalId || "")}"
    >
      ${escapeHtml(label)}
    </button>
  `;
}

async function runAction(button, operation) {
  if (!button || button.disabled) return;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Working...";
  try {
    await operation();
    await loadSnapshot();
  } catch (error) {
    button.textContent = "Retry";
    button.title = error instanceof Error ? error.message : "Action failed";
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      if (button.textContent === "Working...") {
        button.textContent = originalLabel;
      }
    }
  }
}

document.addEventListener("click", (event) => {
  const logTarget = event.target.closest("[data-log-filter]");
  if (logTarget) {
    const nextFilter = logTarget.getAttribute("data-log-filter");
    if (!nextFilter || nextFilter === state.activeLogFilter) return;
    state.activeLogFilter = nextFilter;
    renderLogExperience();
    return;
  }

  const manualTarget = event.target.closest("[data-manual-action]");
  if (manualTarget) {
    const action = manualTarget.getAttribute("data-manual-action");
    const positionId = manualTarget.getAttribute("data-target-id");
    if (!action || !positionId) return;
    void runAction(manualTarget, () => postJson(`/api/manual-positions/${encodeURIComponent(positionId)}`, { action }));
    return;
  }

  const incidentTarget = event.target.closest("[data-incident-action]");
  if (incidentTarget) {
    const action = incidentTarget.getAttribute("data-incident-action");
    const incidentId = incidentTarget.getAttribute("data-target-id");
    if (!action || !incidentId) return;
    void runAction(incidentTarget, () => postJson(`/api/errors/${encodeURIComponent(incidentId)}`, { action }));
    return;
  }

  const mappingTarget = event.target.closest("[data-mapping-action]");
  if (mappingTarget) {
    const action = mappingTarget.getAttribute("data-mapping-action");
    const canonicalId = mappingTarget.getAttribute("data-target-id");
    if (!action || !canonicalId) return;
    void runAction(mappingTarget, () => postJson(`/api/market-mappings/${encodeURIComponent(canonicalId)}`, { action }));
  }
});

if (edgeChartEl && equityChartEl) {
  const resizeObserver = new ResizeObserver(() => renderCharts());
  resizeObserver.observe(edgeChartEl);
  resizeObserver.observe(equityChartEl);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) renderCharts();
});

window.setInterval(() => {
  loadSnapshot().catch((error) => console.error(error));
}, 7000);

loadSnapshot().catch((error) => console.error(error));
connectWebSocket();
