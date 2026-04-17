import { inferStaticApiBase, mixedContentApiWarning, normalizeApiBase } from "./api-base.js";
import { buildActivityAtlasView } from "./activity-atlas-model.js";
import { buildDeskOverview, buildMetricCards, buildOpportunityRows } from "./dashboard-view-model.js";

const boot = window.ARBITER_BOOTSTRAP || {};
const search = new URLSearchParams(window.location.search);
const API_BASE_STORAGE_KEY = "arbiter.apiBase";
const AUTH_TOKEN_STORAGE_KEY = "arbiter.authToken";

function readStorage(storage, key) {
  try {
    return storage?.getItem(key) || "";
  } catch {
    return "";
  }
}

function writeStorage(storage, key, value) {
  try {
    if (!storage) return;
    if (value) {
      storage.setItem(key, value);
    } else {
      storage.removeItem(key);
    }
  } catch {
    // Ignore storage access failures in privacy-restricted contexts.
  }
}

const initialRoute = search.get("route") || boot.routeMode || window.location.pathname || "/";
const initialApiBase = inferStaticApiBase({
  searchParams: search,
  boot,
  storageValue: readStorage(window.localStorage, API_BASE_STORAGE_KEY),
  locationHref: window.location.href,
});
const initialAuthToken = readStorage(window.sessionStorage, AUTH_TOKEN_STORAGE_KEY);
const normalizedInitialRoute = initialRoute.replace(/\/+$/, "");

const state = {
  system: null,
  opportunities: [],
  trades: [],
  manualPositions: [],
  incidents: [],
  mappings: [],
  portfolio: null,
  profitability: null,
  wsConnected: false,
  lastQuoteAt: null,
  activeLogFilter: "all",
  activeLogScope: "all",
  logQuery: "",
  apiBase: initialApiBase,
  authToken: initialAuthToken,
  operatorAuthenticated: false,
  operatorEmail: "",
  routeMode: normalizedInitialRoute === "/ops" || normalizedInitialRoute.endsWith("/ops") ? "ops" : "public",
  websocket: null,
  refreshTimer: null,
  connectionMessage: "",
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
const LOG_ATLAS_LIMITS = {
  total: 48,
  opportunity: 12,
  execution: 10,
  manual: 8,
  incident: 10,
  balance: 6,
  collector: 6,
  mapping: 6,
};

const LOG_SCOPE_DEFINITIONS = {
  all: {
    label: "All activity",
    description: "Every visible atlas event across trading, ops, and infrastructure.",
    categories: FILTER_ORDER.filter((key) => key !== "all"),
  },
  trading: {
    label: "Trading flow",
    description: "Pulse, scanner, and execution activity.",
    categories: ["market", "opportunity", "execution"],
  },
  ops: {
    label: "Ops workflow",
    description: "Manual desk, recovery, and mapping review.",
    categories: ["manual", "incident", "mapping"],
  },
  infrastructure: {
    label: "Infrastructure",
    description: "Funding posture and collector health.",
    categories: ["balance", "collector"],
  },
};

const LOG_SCOPE_ORDER = ["all", "trading", "ops", "infrastructure"];

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
const statusBandEl = document.getElementById("statusBand");
const wsStatusEl = document.getElementById("wsStatus");
const modePillEl = document.getElementById("modePill");
const edgeChartMetaEl = document.getElementById("edgeChartMeta");
const equityChartMetaEl = document.getElementById("equityChartMeta");
const authOverlayEl = document.getElementById("authOverlay");
const authFormEl = document.getElementById("authForm");
const authMessageEl = document.getElementById("authMessage");
const authSubmitEl = document.getElementById("authSubmit");
const authPublicLinkEl = document.getElementById("authPublicLink");
const authEmailEl = document.getElementById("authEmail");
const authPasswordEl = document.getElementById("authPassword");
const connectionOverlayEl = document.getElementById("connectionOverlay");
const connectionFormEl = document.getElementById("connectionForm");
const connectionMessageEl = document.getElementById("connectionMessage");
const connectionResetEl = document.getElementById("connectionReset");
const apiBaseInputEl = document.getElementById("apiBaseInput");
const deskModeTagEl = document.getElementById("deskModeTag");
const apiBasePillEl = document.getElementById("apiBasePill");
const apiConfigButtonEl = document.getElementById("apiConfigButton");
const opsShortcutEl = document.getElementById("opsShortcut");
const logoutButtonEl = document.getElementById("logoutButton");
const dockOpsLinkEl = document.getElementById("dockOpsLink");
const heroTitleEl = document.getElementById("heroTitle");
const heroSubtitleEl = document.getElementById("heroSubtitle");
const heroValueEl = document.getElementById("heroValue");
const heroDeltaEl = document.getElementById("heroDelta");
const heroUpdatedEl = document.getElementById("heroUpdated");
const accessPillEl = document.getElementById("accessPill");
const riskUpdatedEl = document.getElementById("riskUpdated");
const riskScoreBarEl = document.getElementById("riskScoreBar");
const riskSummaryEl = document.getElementById("riskSummary");
const riskSummaryListEl = document.getElementById("riskSummaryList");
const recentTradeCountEl = document.getElementById("recentTradeCount");
const recentTradesRailEl = document.getElementById("recentTradesRail");
const profitabilityPillEl = document.getElementById("profitabilityPill");
const profitabilityVerdictBadgeEl = document.getElementById("profitabilityVerdictBadge");
const profitabilitySummaryEl = document.getElementById("profitabilitySummary");
const profitabilityReasonsEl = document.getElementById("profitabilityReasons");
const portfolioExposureBadgeEl = document.getElementById("portfolioExposureBadge");
const portfolioSummaryEl = document.getElementById("portfolioSummary");
const portfolioListEl = document.getElementById("portfolioList");
const deskMenuEl = document.getElementById("deskMenu");
const logScopeTabsEl = document.getElementById("logScopeTabs");
const logSearchInputEl = document.getElementById("logSearchInput");
const logResultSummaryEl = document.getElementById("logResultSummary");

const formatUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const formatWhole = new Intl.NumberFormat("en-US");
const formatClock = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });

function isStaticFrontend() {
  return Boolean(boot.staticFrontend);
}

function isOpsMode() {
  return state.routeMode === "ops";
}

function hasOperatorAccess() {
  return isOpsMode() && state.operatorAuthenticated;
}

function getPublicHref() {
  if (!isStaticFrontend()) {
    return boot.publicHref || "/";
  }
  const params = new URLSearchParams(window.location.search);
  params.delete("route");
  if (state.apiBase) params.set("api", state.apiBase);
  else params.delete("api");
  const query = params.toString();
  return `./${query ? `?${query}` : ""}`;
}

function getOpsHref() {
  if (!isStaticFrontend()) {
    return boot.opsHref || "/ops";
  }
  const params = new URLSearchParams(window.location.search);
  params.set("route", "/ops");
  if (state.apiBase) params.set("api", state.apiBase);
  else params.delete("api");
  return `./?${params.toString()}`;
}

function syncLocationState() {
  if (!isStaticFrontend() || !window.history?.replaceState) return;
  const params = new URLSearchParams(window.location.search);
  if (isOpsMode()) params.set("route", "/ops");
  else params.delete("route");
  if (state.apiBase) params.set("api", state.apiBase);
  else params.delete("api");
  const query = params.toString();
  window.history.replaceState({}, "", `./${query ? `?${query}` : ""}`);
}

function apiDisplayLabel() {
  if (state.apiBase) return state.apiBase;
  return isStaticFrontend() ? "API not configured" : "same origin";
}

function buildApiUrl(path) {
  const base = state.apiBase || window.location.origin;
  return new URL(path, `${base.replace(/\/+$/, "")}/`).toString();
}

function buildWebSocketUrl() {
  const base = new URL(state.apiBase || window.location.origin);
  const protocol = base.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${base.host}/ws`;
}

function setWsLabel(text, muted = true) {
  if (!wsStatusEl) return;
  wsStatusEl.textContent = text;
  wsStatusEl.classList.toggle("value-muted", muted);
}

function setAuthMessage(message, isError = false) {
  if (!authMessageEl) return;
  authMessageEl.textContent = message || "";
  authMessageEl.classList.toggle("auth-message-error", Boolean(message) && isError);
}

function setConnectionMessage(message, isError = false) {
  state.connectionMessage = message || "";
  if (!connectionMessageEl) return;
  connectionMessageEl.textContent = state.connectionMessage;
  connectionMessageEl.classList.toggle("auth-message-error", Boolean(message) && isError);
}

function showAuthOverlay(message = "") {
  if (!authOverlayEl) return;
  setAuthMessage(message);
  authOverlayEl.classList.remove("hidden");
  authOverlayEl.setAttribute("aria-hidden", "false");
}

function hideAuthOverlay() {
  if (!authOverlayEl) return;
  authOverlayEl.classList.add("hidden");
  authOverlayEl.setAttribute("aria-hidden", "true");
}

function showConnectionOverlay(message = "") {
  if (!connectionOverlayEl) return;
  if (apiBaseInputEl) apiBaseInputEl.value = state.apiBase;
  setConnectionMessage(message);
  connectionOverlayEl.classList.remove("hidden");
  connectionOverlayEl.setAttribute("aria-hidden", "false");
}

function hideConnectionOverlay() {
  if (!connectionOverlayEl) return;
  connectionOverlayEl.classList.add("hidden");
  connectionOverlayEl.setAttribute("aria-hidden", "true");
}

function persistApiBase(apiBase) {
  state.apiBase = normalizeApiBase(apiBase);
  writeStorage(window.localStorage, API_BASE_STORAGE_KEY, state.apiBase);
  syncLocationState();
}

function persistAuthToken(token) {
  state.authToken = token || "";
  writeStorage(window.sessionStorage, AUTH_TOKEN_STORAGE_KEY, state.authToken);
}

function cents(value) {
  return `${Number(value || 0).toFixed(1)}\u00a2`;
}

function pct(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function clamp01(value) {
  return Math.max(0, Math.min(1, Number(value || 0)));
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
  const penalty = Number(collector?.rate_limiter?.remaining_penalty_seconds || 0);
  if (penalty > 0) return "tone-amber";
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

function auditPassRate() {
  return Number(state.system?.audit?.pass_rate || state.system?.execution?.audit?.pass_rate || 0);
}

function activeCooldownCount() {
  return Object.values(state.system?.collectors || {}).filter((collector) => Number(collector?.rate_limiter?.remaining_penalty_seconds || 0) > 0).length;
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

function emptyLogCategoryCounts() {
  return FILTER_ORDER
    .filter((key) => key !== "all")
    .reduce((counts, key) => {
      counts[key] = 0;
      return counts;
    }, {});
}

function buildLogCategoryCounts(entries) {
  return entries.reduce((counts, entry) => {
    if (!entry?.category || !(entry.category in counts)) return counts;
    counts[entry.category] += 1;
    return counts;
  }, emptyLogCategoryCounts());
}

function currentLogScope() {
  return LOG_SCOPE_DEFINITIONS[state.activeLogScope] || LOG_SCOPE_DEFINITIONS.all;
}

function scopeEntries(entries, scopeKey = state.activeLogScope) {
  const scope = LOG_SCOPE_DEFINITIONS[scopeKey] || LOG_SCOPE_DEFINITIONS.all;
  if (scopeKey === "all") return entries;
  const allowedCategories = new Set(scope.categories);
  return entries.filter((entry) => allowedCategories.has(entry.category));
}

function buildLogScopeCounts(entries) {
  return LOG_SCOPE_ORDER.reduce((counts, key) => {
    counts[key] = scopeEntries(entries, key).length;
    return counts;
  }, {});
}

function buildLogSearchText(entry) {
  return [
    entry.title,
    entry.headline,
    entry.narrative,
    entry.footnote,
    ...(entry.tags || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function matchesLogQuery(entry, query) {
  if (!query) return true;
  return buildLogSearchText(entry).includes(query);
}

function buildDeskMenuItems(entries) {
  const profitability = state.profitability || state.system?.profitability;
  const openIncidents = state.incidents.filter((incident) => incident.status !== "resolved").length;
  const routeCount = state.opportunities.length;
  const performanceTrades = state.system?.execution?.total_executions || state.trades.length;
  const riskWarnings = state.portfolio?.violations?.length || 0;
  const infraCount = Object.keys(state.system?.collectors || {}).length + state.mappings.length;
  const opsCount = state.manualPositions.length + openIncidents;
  const activityCount = entries.length;

  return [
    {
      key: "overview",
      href: "#statusBand",
      label: "Overview",
      value: state.wsConnected ? "Live" : (state.lastQuoteAt ? "Warm" : "Loading"),
      copy: `${formatWhole.format(state.system?.counts?.prices || 0)} quotes streaming`,
    },
    {
      key: "performance",
      href: "#performanceSection",
      label: "Performance",
      value: formatWhole.format(performanceTrades),
      copy: "edge curve and equity",
    },
    {
      key: "risk",
      href: "#riskSection",
      label: "Guardrails",
      value: riskWarnings ? formatWhole.format(riskWarnings) : "Clean",
      copy: profitability ? titleCase(profitability.verdict) : "validator loading",
    },
    {
      key: "routes",
      href: "#opportunitiesSection",
      label: "Routes",
      value: formatWhole.format(routeCount),
      copy: "live candidates by edge",
    },
    {
      key: "ops",
      href: hasOperatorAccess() ? "#opsSection" : getOpsHref(),
      label: "Operations",
      value: hasOperatorAccess() ? formatWhole.format(opsCount) : (isOpsMode() ? "Locked" : "Open"),
      copy: hasOperatorAccess() ? "manual queue and incidents" : "operator desk and controls",
    },
    {
      key: "activity",
      href: "#logsSection",
      label: "Activity",
      value: formatWhole.format(activityCount),
      copy: `${currentLogScope().label.toLowerCase()} in view`,
    },
    {
      key: "infra",
      href: "#infraSection",
      label: "Infrastructure",
      value: formatWhole.format(infraCount),
      copy: "collectors and mappings",
    },
  ];
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

  state.opportunities.slice(0, LOG_ATLAS_LIMITS.opportunity).forEach((opp, index) => entries.push(buildOpportunityEntry(opp, index)));
  state.trades.slice(0, LOG_ATLAS_LIMITS.execution).forEach((trade, index) => entries.push(buildTradeEntry(trade, index)));
  state.manualPositions.slice(0, LOG_ATLAS_LIMITS.manual).forEach((position, index) => entries.push(buildManualEntry(position, index)));
  state.incidents.slice(0, LOG_ATLAS_LIMITS.incident).forEach((incident, index) => entries.push(buildIncidentEntry(incident, index)));

  Object.entries(state.system?.balances || {}).slice(0, LOG_ATLAS_LIMITS.balance).forEach(([platform, snapshot], index) => {
    entries.push(buildBalanceEntry(platform, snapshot, index));
  });

  Object.entries(state.system?.collectors || {}).slice(0, LOG_ATLAS_LIMITS.collector).forEach(([name, collector], index) => {
    entries.push(buildCollectorEntry(name, collector, index));
  });

  const nonConfirmedMappings = state.mappings.filter((mapping) => mappingStatus(mapping) !== "confirmed");
  const visibleMappings = nonConfirmedMappings.length
    ? nonConfirmedMappings.slice(0, LOG_ATLAS_LIMITS.mapping)
    : state.mappings.slice(0, LOG_ATLAS_LIMITS.mapping);
  visibleMappings.forEach((mapping, index) => entries.push(buildMappingEntry(mapping, index)));

  return entries
    .sort((left, right) => {
      if (left.synthetic !== right.synthetic) return left.synthetic ? 1 : -1;
      if (right.timestamp !== left.timestamp) return right.timestamp - left.timestamp;
      return left.rank - right.rank;
    })
    .slice(0, LOG_ATLAS_LIMITS.total);
}

function renderDeskMenu(entries = buildLogEntries()) {
  if (!deskMenuEl) return;
  deskMenuEl.innerHTML = buildDeskMenuItems(entries).map((item) => `
    <a class="desk-menu-link" href="${escapeHtml(item.href)}" data-desk-section="${escapeHtml(item.key)}">
      <span class="desk-menu-link-label">${escapeHtml(item.label)}</span>
      <strong class="desk-menu-link-value">${escapeHtml(item.value)}</strong>
      <span class="desk-menu-link-copy">${escapeHtml(item.copy)}</span>
    </a>
  `).join("");
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
      <p class="log-entry-narrative" title="${escapeHtml(entry.narrative)}">${escapeHtml(entry.narrative)}</p>
      <div class="log-entry-tags">
        ${entry.tags.slice(0, 3).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}
      </div>
    </article>
  `;
}

async function requestJson(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
  };
  if (state.authToken) {
    headers.Authorization = `Bearer ${state.authToken}`;
  }

  const response = await fetch(buildApiUrl(path), {
    method: options.method || "GET",
    cache: options.cache || "no-store",
    credentials: options.credentials || "same-origin",
    headers,
    body: options.body,
  });

  if (response.status === 401 && options.allowUnauthorized) {
    return null;
  }

  if (!response.ok) {
    const message = await response.text();
    if (response.status === 401) {
      state.operatorAuthenticated = false;
      state.operatorEmail = "";
      persistAuthToken("");
      renderChrome();
      if (isOpsMode()) {
        showAuthOverlay("Sign in to continue using operator controls.");
      }
    }
    throw new Error(`${path} failed with ${response.status}: ${message}`);
  }

  return response.json();
}

async function fetchJson(path, options = {}) {
  return requestJson(path, options);
}

async function postJson(path, payload, options = {}) {
  return requestJson(path, {
    ...options,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    body: JSON.stringify(payload || {}),
  });
}

async function refreshOperatorSession() {
  const payload = await fetchJson("/api/auth/me", { allowUnauthorized: true });
  state.operatorAuthenticated = Boolean(payload?.authenticated);
  state.operatorEmail = payload?.email || "";
  if (!state.operatorAuthenticated && !state.authToken) {
    state.operatorEmail = "";
  }
  return state.operatorAuthenticated;
}

async function loadSnapshot() {
  const [system, opportunities, trades, incidents, manualPositions, mappings, portfolio, profitability] = await Promise.all([
    fetchJson("/api/system"),
    fetchJson("/api/opportunities"),
    fetchJson("/api/trades"),
    fetchJson("/api/errors"),
    fetchJson("/api/manual-positions"),
    fetchJson("/api/market-mappings"),
    fetchJson("/api/portfolio"),
    fetchJson("/api/profitability"),
  ]);
  state.system = system;
  state.opportunities = opportunities;
  state.trades = trades;
  state.incidents = incidents;
  state.manualPositions = manualPositions;
  state.mappings = mappings;
  state.portfolio = portfolio;
  state.profitability = profitability;
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

function disconnectWebSocket() {
  if (!state.websocket) return;
  const socket = state.websocket;
  state.websocket = null;
  try {
    socket.close();
  } catch {
    // Ignore shutdown races.
  }
}

function connectWebSocket() {
  disconnectWebSocket();

  if (isStaticFrontend() && !state.apiBase) {
    state.wsConnected = false;
    setWsLabel("API needed", true);
    return;
  }

  const socket = new WebSocket(buildWebSocketUrl());
  state.websocket = socket;

  socket.addEventListener("open", () => {
    if (state.websocket !== socket) return;
    state.wsConnected = true;
    setWsLabel("Live", false);
    socket.send(JSON.stringify({ action: "refresh" }));
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "bootstrap" || message.type === "system") {
      state.system = message.payload;
      state.profitability = message.payload?.profitability || state.profitability;
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
    } else if (message.type === "kill_switch") {
      // SAFE-01: state mutation only — plan 03-07 builds the DOM renderer.
      state.safety = { ...(state.safety || {}), killSwitch: message.payload };
    } else if (message.type === "rate_limit_state") {
      state.safety = { ...(state.safety || {}), rateLimits: message.payload };
    } else if (message.type === "one_leg_exposure") {
      state.oneLegExposures = [message.payload, ...(state.oneLegExposures || [])].slice(0, 8);
    } else if (message.type === "shutdown_state") {
      state.shutdown = message.payload;
    } else if (message.type === "heartbeat") {
      state.lastQuoteAt = message.payload.timestamp;
    }
    render();
  });

  socket.addEventListener("close", () => {
    if (state.websocket !== socket) return;
    state.wsConnected = false;
    setWsLabel(state.system ? "Polling" : "Reconnecting", true);
    window.setTimeout(connectWebSocket, 1500);
  });

  socket.addEventListener("error", () => socket.close());
}

function render() {
  const logEntries = buildLogEntries();
  renderChrome();
  renderOverview();
  renderStatusBand();
  renderMetrics();
  renderProfitabilityPanel();
  renderPortfolioPanel();
  renderOpportunities();
  renderManualQueue();
  renderIncidentQueue();
  renderDeskMenu(logEntries);
  renderLogExperience(logEntries);
  renderMappings();
  renderCollectors();
  renderCharts();
}

function renderChrome() {
  const publicHref = getPublicHref();
  const opsHref = getOpsHref();
  const profitability = state.profitability || state.system?.profitability;
  const opsVisible = hasOperatorAccess();

  if (deskModeTagEl) {
    deskModeTagEl.textContent = isOpsMode() ? "Ops desk" : "Public desk";
  }
  if (apiBasePillEl) {
    apiBasePillEl.textContent = `API: ${apiDisplayLabel()}`;
    apiBasePillEl.classList.toggle("utility-pill-warn", isStaticFrontend() && !state.apiBase);
  }
  if (opsShortcutEl) {
    opsShortcutEl.href = isOpsMode() ? publicHref : opsHref;
    opsShortcutEl.textContent = isOpsMode() ? "Back to public desk" : "Open ops desk";
  }
  if (authPublicLinkEl) {
    authPublicLinkEl.href = publicHref;
  }
  if (logoutButtonEl) {
    logoutButtonEl.classList.toggle("hidden", !state.operatorAuthenticated);
  }
  if (heroTitleEl) {
    heroTitleEl.textContent = isOpsMode() ? "Operator trading desk" : "Live trading desk";
  }
  if (heroSubtitleEl) {
    heroSubtitleEl.textContent = isOpsMode()
      ? "Authenticated operator mode unlocks manual queue actions, mapping controls, incident recovery, and production test workflows."
      : "Public read-only mode streams live opportunities, risk posture, and profitability evidence from the core engine.";
  }
  if (accessPillEl) {
    accessPillEl.parentElement?.classList.add("pill-access");
    accessPillEl.title = "";
    if (hasOperatorAccess() && state.operatorEmail) {
      accessPillEl.innerHTML = `
        <span class="value-stack">
          <span class="value-primary">Operator</span>
          <span class="value-secondary" title="${escapeHtml(state.operatorEmail)}">${escapeHtml(state.operatorEmail)}</span>
        </span>
      `;
      accessPillEl.title = state.operatorEmail;
    } else {
      accessPillEl.textContent = isOpsMode() ? "Sign in required" : "Read only";
    }
    accessPillEl.classList.toggle("value-muted", !hasOperatorAccess());
  }
  if (profitabilityPillEl) {
    profitabilityPillEl.textContent = profitability ? titleCase(profitability.verdict) : "Loading";
    profitabilityPillEl.classList.toggle("value-muted", !profitability || profitability.verdict === "collecting_evidence");
  }
  if (dockOpsLinkEl) {
    if (!isOpsMode()) {
      dockOpsLinkEl.href = opsHref;
      dockOpsLinkEl.textContent = "Ops desk";
    } else if (hasOperatorAccess()) {
      dockOpsLinkEl.href = "#opsSection";
      dockOpsLinkEl.textContent = "Manual";
    } else {
      dockOpsLinkEl.href = "#";
      dockOpsLinkEl.textContent = "Sign in";
    }
  }

  document.querySelectorAll("[data-ops-only]").forEach((element) => {
    element.classList.toggle("hidden", !opsVisible);
  });
}

function renderOverview() {
  if (!state.system) {
    if (heroValueEl) heroValueEl.textContent = "Loading";
    if (heroDeltaEl) {
      heroDeltaEl.textContent = "Waiting";
      heroDeltaEl.classList.remove("is-negative");
    }
    if (heroUpdatedEl) heroUpdatedEl.textContent = "Awaiting live data";
    if (riskUpdatedEl) riskUpdatedEl.innerHTML = "Updated<br>Waiting";
    if (riskScoreBarEl) riskScoreBarEl.style.width = "0%";
    if (riskSummaryEl) riskSummaryEl.textContent = "Risk posture is loading.";
    if (riskSummaryListEl) riskSummaryListEl.innerHTML = emptyState("Portfolio and incident context will appear once the API responds.");
    if (recentTradeCountEl) recentTradeCountEl.textContent = "0";
    if (recentTradesRailEl) recentTradesRailEl.innerHTML = emptyState("No recent trades have been published yet.");
    return;
  }

  const overview = buildDeskOverview({
    system: state.system,
    portfolio: state.portfolio,
    profitability: state.profitability || state.system?.profitability,
    trades: state.trades,
    incidents: state.incidents,
    manualPositions: state.manualPositions,
    opportunities: state.opportunities,
    lastQuoteAt: state.lastQuoteAt,
    wsConnected: state.wsConnected,
  }, {
    nowTimestamp: Date.now() / 1000,
  });

  if (heroValueEl) heroValueEl.textContent = overview.heroValue;
  if (heroDeltaEl) {
    heroDeltaEl.textContent = overview.heroDelta;
    heroDeltaEl.classList.toggle("is-negative", overview.heroDelta.startsWith("-"));
  }
  if (heroUpdatedEl) heroUpdatedEl.textContent = overview.heroUpdated;

  if (riskUpdatedEl) riskUpdatedEl.innerHTML = overview.risk.updatedLabel.replace("\n", "<br>");
  if (riskScoreBarEl) riskScoreBarEl.style.width = `${overview.risk.percent}%`;
  if (riskSummaryEl) riskSummaryEl.textContent = overview.risk.summary;
  if (riskSummaryListEl) {
    riskSummaryListEl.innerHTML = overview.risk.items
      .map((item) => compactPanelItem(item.label, item.copy))
      .join("");
  }

  if (recentTradeCountEl) {
    recentTradeCountEl.textContent = formatWhole.format(overview.recentTrades.length);
  }
  if (recentTradesRailEl) {
    recentTradesRailEl.innerHTML = overview.recentTrades.length
      ? overview.recentTrades.map(renderRecentTradeCard).join("")
      : emptyState("No recent trades have settled yet.");
  }
}

function renderRecentTradeCard(trade) {
  const [sizeValue, ...sizeUnits] = String(trade.copy || "").split(" ");
  return `
    <article class="trade-spotlight-card trade-spotlight-card-${escapeHtml(trade.accent)}">
      <div class="trade-spotlight-head">
        <div class="trade-spotlight-dot trade-spotlight-dot-${escapeHtml(trade.accent)}"></div>
        <div>
          <div class="trade-spotlight-status">${escapeHtml(trade.status)}</div>
          <div class="trade-spotlight-time">${escapeHtml(trade.timestampLabel)}</div>
        </div>
      </div>
      <div class="trade-spotlight-title">${escapeHtml(trade.title)}</div>
      <div class="trade-spotlight-route">${escapeHtml(trade.route)}</div>
      <div class="trade-spotlight-footer">
        <strong>${escapeHtml(trade.value)}</strong>
        <span>${escapeHtml(sizeValue)} ${escapeHtml(sizeUnits.join(" "))}</span>
      </div>
    </article>
  `;
}

function renderProfitabilityPanel() {
  const profitability = state.profitability || state.system?.profitability;
  if (!profitabilityVerdictBadgeEl || !profitabilitySummaryEl || !profitabilityReasonsEl) return;
  if (!profitability) {
    profitabilityVerdictBadgeEl.textContent = "Unavailable";
    profitabilitySummaryEl.textContent = "Profitability evidence is still loading.";
    profitabilityReasonsEl.innerHTML = emptyState("The validator has not published a snapshot yet.");
    return;
  }

  const progressPct = Math.round((Number(profitability.progress || 0) * 100));
  profitabilityVerdictBadgeEl.textContent = titleCase(profitability.verdict);
  profitabilitySummaryEl.textContent = profitability.verdict === "validated_profitable"
    ? `The validator has enough evidence to call the current run profitable after ${formatWhole.format(profitability.completed_executions || 0)} completed executions and ${formatUsd.format(profitability.total_realized_pnl || 0)} in realized P&L.`
    : profitability.verdict === "blocked"
      ? "The run is blocked by risk, audit, or incident quality gates. The desk should stay in test mode until those regressions are cleared."
      : profitability.verdict === "not_profitable"
        ? "The validator reached a negative determination. The route inventory is not clearing the profitability bar yet."
        : `The validator is still collecting evidence. ${progressPct}% of the required proof threshold is complete.`;

  const summaryCards = [
    compactPanelItem("Evidence", `${progressPct}% complete against the profitability thresholds.`),
    compactPanelItem("Executions", `${formatWhole.format(profitability.completed_executions || 0)} completed, ${formatWhole.format(profitability.profitable_executions || 0)} profitable, ${formatWhole.format(profitability.losing_executions || 0)} losing.`),
    compactPanelItem("Quality", `${pct(profitability.audit_pass_rate || 0)} audit pass rate with ${pct(1 - Number(profitability.incident_rate || 0))} non-incident execution quality.`),
    ...(profitability.reasons || []).slice(0, 3).map((reason) => compactPanelItem("Gate", reason)),
  ];
  profitabilityReasonsEl.innerHTML = summaryCards.join("");
}

function renderPortfolioPanel() {
  const portfolio = state.portfolio;
  if (!portfolioExposureBadgeEl || !portfolioSummaryEl || !portfolioListEl) return;
  if (!portfolio) {
    portfolioExposureBadgeEl.textContent = "$0.00";
    portfolioSummaryEl.textContent = "Portfolio state is still loading.";
    portfolioListEl.innerHTML = emptyState("Exposure and violation details will appear once the API responds.");
    return;
  }

  const violations = portfolio.violations || [];
  portfolioExposureBadgeEl.textContent = formatUsd.format(portfolio.total_exposure || 0);
  portfolioSummaryEl.textContent = violations.length
    ? `${formatWhole.format(violations.length)} active risk warnings are open across ${formatWhole.format(portfolio.total_open_positions || 0)} positions.`
    : `${formatWhole.format(portfolio.total_open_positions || 0)} open positions are inside the current venue and exposure guardrails.`;

  const venueCards = Object.values(portfolio.by_venue || {}).slice(0, 3).map((venue) =>
    compactPanelItem(
      platformLabel(venue.platform),
      `${formatUsd.format(venue.total_exposure || 0)} exposure across ${formatWhole.format(venue.position_count || 0)} positions${venue.is_low_balance ? " • low balance" : ""}.`
    )
  );
  const violationCards = violations.slice(0, 2).map((violation) =>
    compactPanelItem(titleCase(violation.level || "warning"), violation.message || "Risk violation detected.")
  );
  portfolioListEl.innerHTML = [...violationCards, ...venueCards].join("") || emptyState("No positions or violations are active.");
}

function renderStatusBand() {
  if (!statusBandEl || !state.system) return;

  const auditRate = auditPassRate();
  const openIncidents = state.incidents.filter((incident) => incident.status !== "resolved").length;
  const cooldowns = activeCooldownCount();
  const lastPulse = state.lastQuoteAt || state.system.timestamp;
  const opportunity = state.opportunities[0];

  const strips = [
    {
      tone: "tone-mint",
      label: "Last pulse",
      value: state.lastQuoteAt ? relTime(lastPulse) : "Waiting",
      copy: state.wsConnected
        ? "WebSocket is hot and streaming fresh market events."
        : "Realtime streaming is unavailable, so the desk is falling back to timed refreshes.",
    },
    {
      tone: auditRate >= 0.99 ? "tone-blue" : "tone-amber",
      label: "Math audit",
      value: pct(auditRate),
      copy: `${formatWhole.format(state.system?.audit?.audits_run || 0)} shadow checks compare scanner math, fee totals, and sizing before trust is granted.`,
    },
    {
      tone: openIncidents ? "tone-rose" : "tone-mint",
      label: "Recovery load",
      value: formatWhole.format(openIncidents),
      copy: openIncidents ? "Open incidents still need operator review before the route is comfortable." : "No active recovery incidents are pressuring the trading surface right now.",
    },
    {
      tone: opportunity?.status === "tradable" ? "tone-gold" : "tone-plum",
      label: "Best route",
      value: opportunity ? cents(opportunity.net_edge_cents) : "No route",
      copy: opportunity
        ? `${platformLabel(opportunity.yes_platform)} YES paired with ${platformLabel(opportunity.no_platform)} NO. ${cooldowns ? `${formatWhole.format(cooldowns)} collector cooldowns active.` : "Collectors are flowing cleanly."}`
        : "The scanner is still waiting for a route that clears fees, persistence, and freshness.",
    },
  ];

  statusBandEl.innerHTML = strips.map((strip) => `
    <article class="status-strip ${strip.tone}">
      <div class="status-strip-label">${escapeHtml(strip.label)}</div>
      <div class="status-strip-value">${escapeHtml(strip.value)}</div>
      <div class="status-strip-copy">${escapeHtml(strip.copy)}</div>
    </article>
  `).join("");
}

function renderMetrics() {
  const metrics = document.getElementById("metricGrid");
  const system = state.system;
  if (!metrics || !system) return;

  modePillEl.textContent = system.mode === "live" ? "Live" : "Dry Run";

  const cards = buildMetricCards({
    system: state.system,
    portfolio: state.portfolio,
    profitability: state.profitability || state.system?.profitability,
    trades: state.trades,
  });

  metrics.innerHTML = cards
    .map((card) => `
      <article class="metric-card">
        <div class="metric-label">${escapeHtml(card.label)}</div>
        <div class="metric-value">${escapeHtml(card.value)}</div>
        <div class="metric-meta">${escapeHtml(card.meta)}</div>
      </article>
    `)
    .join("");

  const bestEdgeLabelEl = document.getElementById("bestEdgeLabel");
  if (bestEdgeLabelEl) {
    bestEdgeLabelEl.textContent = cents(system.scanner?.best_edge_cents || 0);
  }
}

function renderOpportunities() {
  const container = document.getElementById("opportunityList");
  document.getElementById("opportunityCount").textContent = formatWhole.format(state.opportunities.length);
  if (!container) return;
  if (!state.opportunities.length) {
    container.innerHTML = emptyState("No fee-positive opportunities are active right now.");
    return;
  }

  const rows = buildOpportunityRows({
    opportunities: state.opportunities.slice(0, 12),
    system: state.system,
    nowTimestamp: Date.now() / 1000,
  });

  container.innerHTML = rows.map((row) => `
    <article class="blotter-row blotter-row-${escapeHtml(row.status)}">
      <div class="blotter-row-main">
        <div class="blotter-row-titleblock">
          <div class="blotter-row-title">${escapeHtml(row.title)}</div>
          <div class="blotter-row-subtitle">${escapeHtml(row.route)}</div>
        </div>
        <div class="blotter-row-metrics">
          <div class="blotter-cell">
            <span class="blotter-cell-label">Edge</span>
            <strong>${escapeHtml(row.netEdgeLabel)}</strong>
          </div>
          <div class="blotter-cell">
            <span class="blotter-cell-label">Max P&L</span>
            <strong>${escapeHtml(row.maxProfitLabel)}</strong>
          </div>
          <div class="blotter-cell">
            <span class="blotter-cell-label">Confidence</span>
            <strong>${escapeHtml(row.confidenceLabel)}</strong>
          </div>
          <div class="blotter-cell">
            <span class="blotter-cell-label">Freshness</span>
            <strong>${escapeHtml(row.freshnessLabel)}</strong>
          </div>
        </div>
      </div>
      <div class="blotter-row-side">
        <span class="${statusClass(row.status)}">${escapeHtml(row.statusLabel)}</span>
        <div class="blotter-chip-row">
          <span class="blotter-chip">${escapeHtml(`${row.scansLabel} scans`)}</span>
          <span class="blotter-chip">${escapeHtml(`${row.quantityLabel} qty`)}</span>
          <span class="blotter-chip">${escapeHtml(`${row.liquidityLabel} liquid`)}</span>
          <span class="blotter-chip">${escapeHtml(row.updatedLabel)}</span>
        </div>
      </div>
    </article>
  `).join("");
}

function renderManualQueue() {
  const container = document.getElementById("manualQueue");
  const countEl = document.getElementById("manualCount");
  if (countEl) countEl.textContent = formatWhole.format(state.manualPositions.length);
  if (!container) return;
  if (!hasOperatorAccess()) {
    container.innerHTML = emptyState("Sign in to operator mode to use manual queue actions.");
    return;
  }
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
  if (!hasOperatorAccess()) {
    container.innerHTML = emptyState("Sign in to operator mode to resolve incidents from the desk.");
    return;
  }
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

function renderLegCard(label, platform, price, fee, marketId, feeRate) {
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
      <div class="leg-detail-row">
        <span>${escapeHtml(feeRate ? `Rate ${pct(feeRate)}` : "Variable fee")}</span>
        <span>${escapeHtml(`${platformLabel(platform)} contract`)}</span>
      </div>
    </div>
  `;
}

function renderLogExperience(entries = buildLogEntries()) {
  const activityView = buildActivityAtlasView({
    entries,
    activeScope: state.activeLogScope,
    activeFilter: state.activeLogFilter,
    query: state.logQuery,
    scopeDefinitions: LOG_SCOPE_DEFINITIONS,
    filterOrder: FILTER_ORDER,
  });
  const scope = activityView.scope;
  const scopeCounts = activityView.scopeCounts;
  const categoryCounts = activityView.categoryCounts;
  const scopedEntries = activityView.scopedEntries;
  const filteredEntries = activityView.filteredEntries;
  const normalizedQuery = activityView.query;
  if (state.activeLogFilter !== activityView.activeFilter) {
    state.activeLogFilter = activityView.activeFilter;
  }

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

  if (logScopeTabsEl) {
    logScopeTabsEl.innerHTML = LOG_SCOPE_ORDER.map((key) => {
      const scopeDefinition = LOG_SCOPE_DEFINITIONS[key];
      return `
        <button
          type="button"
          class="log-scope-chip ${state.activeLogScope === key ? "is-active" : ""}"
          data-log-scope="${escapeHtml(key)}"
          aria-pressed="${state.activeLogScope === key ? "true" : "false"}"
        >
          <span>${escapeHtml(scopeDefinition.label)}</span>
          <strong>${escapeHtml(formatWhole.format(scopeCounts[key] || 0))}</strong>
        </button>
      `;
    }).join("");
  }

  if (logSearchInputEl && logSearchInputEl.value !== state.logQuery) {
    logSearchInputEl.value = state.logQuery;
  }

  const activeCategories = Object.values(categoryCounts).filter((count) => count > 0).length;
  if (categoryCount) {
    categoryCount.textContent = formatWhole.format(activeCategories);
  }

  if (categoryAtlas) {
    categoryAtlas.innerHTML = scope.categories.map((key) => {
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
    filterBar.innerHTML = activityView.filterItems.map((item) => {
      const key = item.key;
      const count = item.count;
      const label = key === "all" ? "All activity" : LOG_DEFINITIONS[key].label;
      return `
        <button type="button" class="log-filter-chip ${state.activeLogFilter === key ? "is-active" : ""}" data-log-filter="${escapeHtml(key)}">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(formatWhole.format(count || 0))}</strong>
        </button>
      `;
    }).join("");
  }

  if (logResultSummaryEl) {
    if (normalizedQuery) {
      logResultSummaryEl.textContent = `Compact console showing ${formatWhole.format(filteredEntries.length)} of ${formatWhole.format(scopedEntries.length)} events in ${scope.label.toLowerCase()} for "${state.logQuery}".`;
    } else {
      logResultSummaryEl.textContent = `Compact console showing ${formatWhole.format(filteredEntries.length)} events in ${scope.label.toLowerCase()}.`;
    }
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
    timeline.innerHTML = emptyState(query
      ? "No events match the current activity search."
      : "No events match this activity view right now.");
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
      ${hasOperatorAccess() ? `<div class="action-row">
        ${mappingStatus(mapping) !== "confirmed" ? renderActionButton("Confirm match", "confirm", "mapping", mapping.canonical_id, mapping.canonical_id) : ""}
        ${mapping.allow_auto_trade ? renderActionButton("Hold auto-trade", "disable_auto_trade", "mapping", mapping.canonical_id, mapping.canonical_id, true) : renderActionButton("Enable auto-trade", "enable_auto_trade", "mapping", mapping.canonical_id, mapping.canonical_id)}
        ${mappingStatus(mapping) !== "review" ? renderActionButton("Mark review", "review", "mapping", mapping.canonical_id, mapping.canonical_id, true) : ""}
      </div>` : ""}
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
    const remainingPenalty = Number(collector?.rate_limiter?.remaining_penalty_seconds || 0);
    const limiterMeta = remainingPenalty > 0
      ? `Rate limit cooldown ${remainingPenalty.toFixed(1)}s`
      : `Rate limiter ${formatWhole.format(collector?.rate_limiter?.available_tokens || 0)} tokens`;
    const collectorStatus = circuitState === "open"
      ? "critical"
      : remainingPenalty > 0 || (collector.total_errors || 0) > 0 || (collector.consecutive_errors || 0) > 0
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
          ${escapeHtml(`Circuit ${titleCase(circuitState)} • ${limiterMeta}`)}
        </div>
      </article>
    `;
  }).join("");
}

function renderCharts() {
  const scannerSeries = state.system?.series?.scanner || [];
  const equitySeries = state.system?.series?.equity || [];
  renderChartMeta(edgeChartMetaEl, scannerSeries, "best_edge_cents", "\u00a2");
  renderChartMeta(equityChartMetaEl, equitySeries, "equity", "$");
  drawLineChart(edgeChartEl, scannerSeries, {
    valueKey: "best_edge_cents",
    color: "#8b8f97",
    fill: "rgba(139, 143, 151, 0.16)",
    axisSuffix: "\u00a2",
    focusColor: "#8b8f97",
  });
  drawLineChart(equityChartEl, equitySeries, {
    valueKey: "equity",
    color: "#d7ff1f",
    fill: "rgba(215, 255, 31, 0.18)",
    axisPrefix: "$",
    focusColor: "#d7ff1f",
  });
}

function renderChartMeta(target, series, valueKey, unitPrefix = "") {
  if (!target) return;
  if (!series.length) {
    target.innerHTML = "";
    return;
  }

  const values = series.map((point) => Number(point[valueKey] || 0));
  const latest = values[values.length - 1] || 0;
  const high = Math.max(...values);
  const low = Math.min(...values);
  const average = values.reduce((sum, value) => sum + value, 0) / Math.max(values.length, 1);
  const lastTimestamp = series[series.length - 1]?.timestamp || 0;

  const formatter = unitPrefix === "$"
    ? (value) => formatUsd.format(value)
    : (value) => `${Number(value).toFixed(1)}${unitPrefix}`;

  target.innerHTML = [
    { label: "Latest", value: formatter(latest) },
    { label: "High", value: formatter(high) },
    { label: "Low", value: formatter(low) },
    { label: "Average", value: formatter(average) },
    { label: "Updated", value: lastTimestamp ? relTime(lastTimestamp) : "Waiting" },
  ]
    .map((item) => `
      <span class="chart-meta-pill">
        <strong class="chart-meta-value">${escapeHtml(item.value)}</strong>
        <span class="chart-meta-label">${escapeHtml(item.label)}</span>
      </span>
    `)
    .join("");
}

function drawLineChart(target, points, options) {
  if (!target) return;
  const rect = target.getBoundingClientRect();
  const width = rect.width || target.clientWidth;
  const computedHeight = Number.parseFloat(window.getComputedStyle(target).height || "0");
  const rawHeight = rect.height || target.clientHeight || computedHeight || 280;
  const height = Math.min(Math.max(rawHeight, 220), 340);
  if (!width) {
    window.requestAnimationFrame(() => drawLineChart(target, points, options));
    return;
  }

  if (!points.length) {
    target.innerHTML = emptyState("Waiting for live series data.");
    return;
  }

  const padding = { top: 18, right: 28, bottom: 28, left: 20 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  const values = points.map((point) => Number(point[options.valueKey] || 0));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1e-6);

  const coords = points.map((point, index) => {
    const x = padding.left + (innerWidth * (points.length === 1 ? 0 : index / (points.length - 1)));
    const normalized = (max - min) < 1e-6 ? 0.5 : ((Number(point[options.valueKey] || 0) - min) / range);
    const y = padding.top + innerHeight - normalized * innerHeight;
    return [x, y];
  });

  const linePath = buildSmoothPath(coords);
  const fillPath = `${linePath} L ${padding.left + innerWidth} ${padding.top + innerHeight} L ${padding.left} ${padding.top + innerHeight} Z`;
  const gridLines = [0, 0.5, 1].map((fraction) => {
    const y = padding.top + innerHeight * fraction;
    return `<line class="chart-grid-line" x1="${padding.left}" y1="${y}" x2="${padding.left + innerWidth}" y2="${y}"></line>`;
  }).join("");
  const minLabel = formatChartAxisValue(min, options);
  const maxLabel = formatChartAxisValue(max, options);
  const lastLabel = formatClock.format(new Date((points[points.length - 1]?.timestamp || Date.now() / 1000) * 1000));
  const [lastX, lastY] = coords[coords.length - 1];
  const latestValue = Number(values[values.length - 1] || 0);
  const latestText = formatChartAxisValue(latestValue, options);
  const focusLabelWidth = Math.max(78, latestText.length * 7 + 18);
  const focusLabelHeight = 24;
  const preferLeft = lastX > padding.left + innerWidth - focusLabelWidth - 12;
  const labelCenterX = preferLeft
    ? Math.max(padding.left + focusLabelWidth / 2, lastX - 18 - focusLabelWidth / 2)
    : Math.min(padding.left + innerWidth - focusLabelWidth / 2, lastX + 18 + focusLabelWidth / 2);
  const labelCenterY = lastY < padding.top + focusLabelHeight + 8
    ? Math.min(padding.top + innerHeight - focusLabelHeight / 2, lastY + 20)
    : Math.max(padding.top + focusLabelHeight / 2, lastY - 20);
  const labelRectX = labelCenterX - focusLabelWidth / 2;
  const labelRectY = labelCenterY - focusLabelHeight / 2;

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
      <circle class="chart-focus-halo" cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="9"></circle>
      <circle class="chart-focus-core" cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="4.5" style="--chart-focus:${options.focusColor || options.color}"></circle>
      <rect class="chart-focus-pill" x="${labelRectX.toFixed(2)}" y="${labelRectY.toFixed(2)}" width="${focusLabelWidth.toFixed(2)}" height="${focusLabelHeight}" rx="12"></rect>
      <text class="chart-focus-label" x="${labelCenterX.toFixed(2)}" y="${labelCenterY.toFixed(2)}" text-anchor="middle" dominant-baseline="middle">${escapeHtml(latestText)}</text>
      <text class="chart-axis-label" x="${padding.left}" y="${padding.top + 12}">${escapeHtml(maxLabel)}</text>
      <text class="chart-axis-label" x="${padding.left}" y="${padding.top + innerHeight - 6}">${escapeHtml(minLabel)}</text>
      <text class="chart-axis-label" x="${padding.left + innerWidth}" y="${height - 10}" text-anchor="end">${escapeHtml(lastLabel)}</text>
    </svg>
  `;
}

function formatChartAxisValue(value, options) {
  if ((options.axisPrefix || "") === "$") {
    return formatUsd.format(value || 0);
  }
  return `${Number(value || 0).toFixed(1)}${options.axisSuffix || ""}`;
}

function buildSmoothPath(coords) {
  if (!coords.length) return "";
  if (coords.length === 1) {
    const [x, y] = coords[0];
    return `M ${x.toFixed(2)} ${y.toFixed(2)}`;
  }

  let path = `M ${coords[0][0].toFixed(2)} ${coords[0][1].toFixed(2)}`;
  for (let index = 0; index < coords.length - 1; index += 1) {
    const previous = coords[index - 1] || coords[index];
    const current = coords[index];
    const next = coords[index + 1];
    const afterNext = coords[index + 2] || next;

    const cp1x = current[0] + (next[0] - previous[0]) / 6;
    const cp1y = current[1] + (next[1] - previous[1]) / 6;
    const cp2x = next[0] - (afterNext[0] - current[0]) / 6;
    const cp2y = next[1] - (afterNext[1] - current[1]) / 6;

    path += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)}, ${cp2x.toFixed(2)} ${cp2y.toFixed(2)}, ${next[0].toFixed(2)} ${next[1].toFixed(2)}`;
  }
  return path;
}

function compactPanelItem(label, copy) {
  return `
    <article class="stack-item compact-item">
      <div class="stack-item-header">
        <div class="stack-item-title">${escapeHtml(label)}</div>
      </div>
      <div class="stack-item-meta">${escapeHtml(copy)}</div>
    </article>
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
      <span class="action-button-label">${escapeHtml(label)}</span>
    </button>
  `;
}

async function runAction(button, operation) {
  if (!button || button.disabled) return;
  const labelEl = button.querySelector(".action-button-label");
  const setLabel = (value) => {
    if (labelEl) {
      labelEl.textContent = value;
    } else {
      button.textContent = value;
    }
  };
  const originalLabel = labelEl?.textContent || button.textContent;
  button.disabled = true;
  setLabel("Working...");
  try {
    await operation();
    await loadSnapshot();
  } catch (error) {
    setLabel("Retry");
    button.title = error instanceof Error ? error.message : "Action failed";
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      if ((labelEl?.textContent || button.textContent) === "Working...") {
        setLabel(originalLabel);
      }
    }
  }
}

document.addEventListener("click", (event) => {
  const logScopeTarget = event.target.closest("[data-log-scope]");
  if (logScopeTarget) {
    const nextScope = logScopeTarget.getAttribute("data-log-scope");
    if (!nextScope || nextScope === state.activeLogScope) return;
    state.activeLogScope = nextScope;
    state.activeLogFilter = "all";
    render();
    return;
  }

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
    if (!hasOperatorAccess()) {
      showAuthOverlay("Sign in to use manual desk controls.");
      return;
    }
    const action = manualTarget.getAttribute("data-manual-action");
    const positionId = manualTarget.getAttribute("data-target-id");
    if (!action || !positionId) return;
    void runAction(manualTarget, () => postJson(`/api/manual-positions/${encodeURIComponent(positionId)}`, { action }));
    return;
  }

  const incidentTarget = event.target.closest("[data-incident-action]");
  if (incidentTarget) {
    if (!hasOperatorAccess()) {
      showAuthOverlay("Sign in to resolve incidents from the ops desk.");
      return;
    }
    const action = incidentTarget.getAttribute("data-incident-action");
    const incidentId = incidentTarget.getAttribute("data-target-id");
    if (!action || !incidentId) return;
    void runAction(incidentTarget, () => postJson(`/api/errors/${encodeURIComponent(incidentId)}`, { action }));
    return;
  }

  const mappingTarget = event.target.closest("[data-mapping-action]");
  if (mappingTarget) {
    if (!hasOperatorAccess()) {
      showAuthOverlay("Sign in to change mapping state.");
      return;
    }
    const action = mappingTarget.getAttribute("data-mapping-action");
    const canonicalId = mappingTarget.getAttribute("data-target-id");
    if (!action || !canonicalId) return;
    void runAction(mappingTarget, () => postJson(`/api/market-mappings/${encodeURIComponent(canonicalId)}`, { action }));
    return;
  }

  if (event.target === apiConfigButtonEl) {
    showConnectionOverlay(state.connectionMessage);
    return;
  }

  if (event.target === dockOpsLinkEl && isOpsMode() && !hasOperatorAccess()) {
    event.preventDefault();
    showAuthOverlay("Sign in to unlock the operator desk.");
    return;
  }

  if (event.target === logoutButtonEl) {
    void (async () => {
      try {
        await postJson("/api/auth/logout", {});
      } catch {
        // Local cleanup still matters even if the server session is already gone.
      }
      persistAuthToken("");
      state.operatorAuthenticated = false;
      state.operatorEmail = "";
      renderChrome();
      if (isOpsMode()) {
        showAuthOverlay("Signed out. Sign in again to continue using the ops desk.");
      }
    })();
  }
});

if (logSearchInputEl) {
  logSearchInputEl.addEventListener("input", (event) => {
    state.logQuery = event.target.value || "";
    renderLogExperience();
  });
}

if (authFormEl) {
  authFormEl.addEventListener("submit", (event) => {
    event.preventDefault();
    const email = authEmailEl?.value.trim().toLowerCase() || "";
    const password = authPasswordEl?.value || "";
    if (!email || !password) {
      setAuthMessage("Enter both email and password.", true);
      return;
    }

    void (async () => {
      if (authSubmitEl) authSubmitEl.disabled = true;
      setAuthMessage("Signing in...");
      try {
        const payload = await postJson("/api/auth/login", { email, password });
        persistAuthToken(payload.token || "");
        const authenticated = await refreshOperatorSession();
        if (!authenticated) {
          throw new Error("Operator session was not confirmed by the API.");
        }
        state.operatorAuthenticated = true;
        state.operatorEmail = payload.email || email;
        hideAuthOverlay();
        setAuthMessage("");
        renderChrome();
        await loadSnapshot();
        connectWebSocket();
      } catch (error) {
        setAuthMessage(error instanceof Error ? error.message : "Sign-in failed.", true);
      } finally {
        if (authSubmitEl) authSubmitEl.disabled = false;
      }
    })();
  });
}

if (connectionFormEl) {
  connectionFormEl.addEventListener("submit", (event) => {
    event.preventDefault();
    persistApiBase(apiBaseInputEl?.value || "");
    setConnectionMessage(state.apiBase ? "Saved. Reconnecting to the selected API." : "Using same-origin API.");
    hideConnectionOverlay();
    void refreshAllData();
  });
}

if (connectionResetEl) {
  connectionResetEl.addEventListener("click", () => {
    persistApiBase("");
    if (apiBaseInputEl) apiBaseInputEl.value = "";
    setConnectionMessage(isStaticFrontend() ? "Connection cleared. Configure an API source to use the static dashboard." : "Using same-origin API.");
    hideConnectionOverlay();
    void refreshAllData();
  });
}

if (edgeChartEl && equityChartEl) {
  const resizeObserver = new ResizeObserver(() => renderCharts());
  resizeObserver.observe(edgeChartEl);
  resizeObserver.observe(equityChartEl);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) renderCharts();
});

async function refreshAllData() {
  if (isStaticFrontend() && !state.apiBase) {
    state.system = null;
    state.portfolio = null;
    state.profitability = null;
    state.opportunities = [];
    state.trades = [];
    state.manualPositions = [];
    state.incidents = [];
    state.mappings = [];
    state.wsConnected = false;
    setWsLabel("API needed", true);
    render();
    showConnectionOverlay("Set the backend API source before using the static dashboard.");
    return;
  }

  const connectionIssue = isStaticFrontend() ? mixedContentApiWarning(window.location.href, state.apiBase) : "";
  if (connectionIssue) {
    state.system = null;
    state.portfolio = null;
    state.profitability = null;
    state.opportunities = [];
    state.trades = [];
    state.manualPositions = [];
    state.incidents = [];
    state.mappings = [];
    state.wsConnected = false;
    setWsLabel("HTTPS blocked", true);
    render();
    showConnectionOverlay(connectionIssue);
    return;
  }

  try {
    if (isOpsMode() || state.authToken) {
      await refreshOperatorSession();
    } else {
      state.operatorAuthenticated = false;
      state.operatorEmail = "";
    }
    if (isOpsMode() && !state.operatorAuthenticated) {
      showAuthOverlay("Sign in to unlock the operator desk.");
    } else {
      hideAuthOverlay();
    }
    await loadSnapshot();
    hideConnectionOverlay();
    connectWebSocket();
  } catch (error) {
    state.wsConnected = false;
    setWsLabel("Offline", true);
    render();
    if (isStaticFrontend()) {
      showConnectionOverlay(error instanceof Error ? error.message : "Unable to reach the selected API.");
    } else {
      console.error(error);
    }
  }
}

function startPolling() {
  if (state.refreshTimer) return;
  state.refreshTimer = window.setInterval(() => {
    if (isStaticFrontend() && !state.apiBase) return;
    loadSnapshot().catch((error) => console.error(error));
  }, 7000);
}

renderChrome();
setWsLabel("Connecting", true);
startPolling();
void refreshAllData();
