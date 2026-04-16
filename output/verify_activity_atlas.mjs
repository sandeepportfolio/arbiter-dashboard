import { chromium, devices } from "playwright";
import fs from "node:fs";

const NOW = 1776340800;
const AUTH_TOKEN = "demo-token";

function buildPayloads() {
  return {
    system: {
      timestamp: NOW,
      mode: "live",
      tracked_markets: {
        "btc-100k-2026": {},
        "fed-cut-july": {},
        "house-majority": {},
      },
      scanner: {
        tradable_opportunities: 9,
        active_opportunities: 16,
        best_edge_cents: 18.4,
        persistence_scans: 4,
        max_quote_age_seconds: 15,
        published: 28,
      },
      execution: {
        total_pnl: 1859.48,
        total_executions: 128,
        audit: { pass_rate: 0.982 },
      },
      audit: {
        audits_run: 2048,
        pass_rate: 0.982,
      },
      balances: {
        kalshi: { timestamp: NOW - 80, balance: 12400, is_low: false },
        polymarket: { timestamp: NOW - 75, balance: 9100, is_low: false },
        predictit: { timestamp: NOW - 65, balance: 45, is_low: true },
      },
      collectors: {
        kalshi: {
          total_fetches: 5420,
          total_errors: 0,
          consecutive_errors: 0,
          rate_limiter: { available_tokens: 118, remaining_penalty_seconds: 0 },
          circuit: { state: "closed" },
        },
        polymarket: {
          total_fetches: 5362,
          total_errors: 2,
          consecutive_errors: 0,
          rate_limiter: { available_tokens: 87, remaining_penalty_seconds: 12 },
          clob_circuit: { state: "half_open" },
        },
      },
      counts: {
        prices: 5200,
        incidents: 2,
      },
      series: {
        scanner: [
          { timestamp: NOW - 3600, best_edge_cents: 7.2 },
          { timestamp: NOW - 3000, best_edge_cents: 8.6 },
          { timestamp: NOW - 2400, best_edge_cents: 10.1 },
          { timestamp: NOW - 1800, best_edge_cents: 11.4 },
          { timestamp: NOW - 1200, best_edge_cents: 13.1 },
          { timestamp: NOW - 600, best_edge_cents: 15.9 },
          { timestamp: NOW - 120, best_edge_cents: 18.4 },
        ],
        equity: [
          { timestamp: NOW - 3600, equity: 40100 },
          { timestamp: NOW - 3000, equity: 40520 },
          { timestamp: NOW - 2400, equity: 40980 },
          { timestamp: NOW - 1800, equity: 41460 },
          { timestamp: NOW - 1200, equity: 41820 },
          { timestamp: NOW - 600, equity: 42090 },
          { timestamp: NOW - 20, equity: 42150 },
        ],
      },
    },
    opportunities: [
      {
        canonical_id: "btc-100k-2026",
        description: "BTC to 100k before year-end",
        status: "tradable",
        yes_platform: "kalshi",
        no_platform: "polymarket",
        yes_price: 0.44,
        no_price: 0.37,
        gross_edge: 0.19,
        total_fees: 0.006,
        net_edge: 0.184,
        net_edge_cents: 18.4,
        max_profit_usd: 736.0,
        confidence: 0.94,
        persistence_count: 4,
        quote_age_seconds: 1.8,
        suggested_qty: 4000,
        min_available_liquidity: 9600,
        mapping_status: "confirmed",
        requires_manual: false,
        timestamp: NOW - 18,
      },
      {
        canonical_id: "fed-cut-july",
        description: "Fed rate cut by July",
        status: "manual",
        yes_platform: "predictit",
        no_platform: "kalshi",
        yes_price: 0.52,
        no_price: 0.34,
        gross_edge: 0.14,
        total_fees: 0.018,
        net_edge: 0.122,
        net_edge_cents: 12.2,
        max_profit_usd: 590.4,
        confidence: 0.88,
        persistence_count: 4,
        quote_age_seconds: 3.4,
        suggested_qty: 2100,
        min_available_liquidity: 4200,
        mapping_status: "review",
        requires_manual: true,
        timestamp: NOW - 25,
      },
      {
        canonical_id: "house-majority",
        description: "House majority control",
        status: "review",
        yes_platform: "polymarket",
        no_platform: "kalshi",
        yes_price: 0.59,
        no_price: 0.28,
        gross_edge: 0.13,
        total_fees: 0.009,
        net_edge: 0.121,
        net_edge_cents: 12.1,
        max_profit_usd: 488.2,
        confidence: 0.81,
        persistence_count: 2,
        quote_age_seconds: 5.8,
        suggested_qty: 1800,
        min_available_liquidity: 3500,
        mapping_status: "review",
        requires_manual: false,
        timestamp: NOW - 31,
      },
      {
        canonical_id: "oil-above-95",
        description: "Oil above 95 in Q3",
        status: "stale",
        yes_platform: "kalshi",
        no_platform: "polymarket",
        yes_price: 0.31,
        no_price: 0.47,
        gross_edge: 0.07,
        total_fees: 0.012,
        net_edge: 0.058,
        net_edge_cents: 5.8,
        max_profit_usd: 119.0,
        confidence: 0.56,
        persistence_count: 1,
        quote_age_seconds: 18.2,
        suggested_qty: 900,
        min_available_liquidity: 1600,
        mapping_status: "candidate",
        requires_manual: false,
        timestamp: NOW - 42,
      },
      {
        canonical_id: "jobs-soft-landing",
        description: "Soft landing jobs print",
        status: "tradable",
        yes_platform: "kalshi",
        no_platform: "polymarket",
        yes_price: 0.42,
        no_price: 0.39,
        gross_edge: 0.15,
        total_fees: 0.007,
        net_edge: 0.143,
        net_edge_cents: 14.3,
        max_profit_usd: 401.7,
        confidence: 0.9,
        persistence_count: 3,
        quote_age_seconds: 2.1,
        suggested_qty: 2500,
        min_available_liquidity: 5200,
        mapping_status: "confirmed",
        requires_manual: false,
        timestamp: NOW - 48,
      },
      {
        canonical_id: "treasury-5pct",
        description: "Treasury stays above 5%",
        status: "tradable",
        yes_platform: "polymarket",
        no_platform: "kalshi",
        yes_price: 0.46,
        no_price: 0.35,
        gross_edge: 0.17,
        total_fees: 0.009,
        net_edge: 0.161,
        net_edge_cents: 16.1,
        max_profit_usd: 522.4,
        confidence: 0.92,
        persistence_count: 4,
        quote_age_seconds: 2.8,
        suggested_qty: 2800,
        min_available_liquidity: 6000,
        mapping_status: "confirmed",
        requires_manual: false,
        timestamp: NOW - 57,
      },
    ],
    trades: [
      {
        arb_id: "arb-1042",
        status: "filled",
        timestamp: NOW - 80,
        realized_pnl: 85.25,
        opportunity: { description: "BTC to 100k before year-end", canonical_id: "btc-100k-2026" },
        leg_yes: { platform: "kalshi", price: 0.47, quantity: 140 },
        leg_no: { platform: "predictit", price: 0.4, quantity: 140 },
        notes: ["filled cleanly"],
      },
      {
        arb_id: "arb-1041",
        status: "submitted",
        timestamp: NOW - 96,
        realized_pnl: 12.4,
        opportunity: { description: "Fed rate cut by July", canonical_id: "fed-cut-july" },
        leg_yes: { platform: "predictit", price: 0.52, quantity: 90 },
        leg_no: { platform: "kalshi", price: 0.34, quantity: 90 },
        notes: ["awaiting hedge confirmation"],
      },
      {
        arb_id: "arb-1039",
        status: "failed",
        timestamp: NOW - 112,
        realized_pnl: -19.57,
        opportunity: { description: "House majority control", canonical_id: "house-majority" },
        leg_yes: { platform: "polymarket", price: 0.61, quantity: 80 },
        leg_no: { platform: "kalshi", price: 0.29, quantity: 80 },
        notes: ["hedge leg timed out"],
      },
    ],
    errors: [
      {
        incident_id: "inc-77",
        status: "open",
        severity: "critical",
        message: "One-leg fill mismatch requires operator review",
        canonical_id: "house-majority",
        arb_id: "arb-1039",
        timestamp: NOW - 240,
        metadata: {
          original_yes: 0.61,
          current_yes: 0.64,
          original_no: 0.29,
          current_no: 0.31,
        },
        resolution_note: "Still waiting for operator resolution.",
      },
      {
        incident_id: "inc-65",
        status: "resolved",
        severity: "warning",
        message: "Collector recovered after cooldown",
        canonical_id: "btc-100k-2026",
        arb_id: "arb-1038",
        timestamp: NOW - 1800,
        metadata: { reason: "temporary rate-limit cooldown" },
        resolution_note: "Auto-recovered after retry window.",
      },
    ],
    "manual-positions": [
      {
        position_id: "manual-201",
        canonical_id: "fed-cut-july",
        description: "PredictIt-assisted July rate cut route",
        yes_platform: "predictit",
        no_platform: "kalshi",
        yes_price: 0.52,
        no_price: 0.34,
        quantity: 90,
        status: "awaiting-entry",
        timestamp: NOW - 420,
        instructions: "Place the PredictIt YES leg first, verify quantity, then confirm entry in Arbiter.",
        note: "Awaiting operator acknowledgement.",
      },
    ],
    "market-mappings": [
      {
        canonical_id: "btc-100k-2026",
        description: "BTC to 100k before year-end",
        status: "confirmed",
        allow_auto_trade: true,
        notes: "Confirmed mapping across both venues.",
        kalshi: "KXBTC100K",
        polymarket: "PM-BTC-100K",
        predictit: "PI-BTC-100K",
      },
      {
        canonical_id: "fed-cut-july",
        description: "Fed rate cut by July",
        status: "review",
        allow_auto_trade: false,
        review_note: "PredictIt wording still needs manual confirmation.",
        kalshi: "KFEDCUTJUL",
        predictit: "PI-FEDCUT-JULY",
      },
    ],
    portfolio: {
      total_exposure: 21400,
      total_open_positions: 12,
      violations: [{ level: "warning", message: "Concentration on election series" }],
      by_venue: {
        kalshi: { platform: "kalshi", total_exposure: 12400, position_count: 7, is_low_balance: false },
        polymarket: { platform: "polymarket", total_exposure: 9000, position_count: 5, is_low_balance: false },
        predictit: { platform: "predictit", total_exposure: 0, position_count: 0, is_low_balance: true },
      },
    },
    profitability: {
      verdict: "collecting_evidence",
      progress: 0.68,
      completed_executions: 128,
      profitable_executions: 84,
      losing_executions: 44,
      total_realized_pnl: 1859.48,
      audit_pass_rate: 0.982,
      incident_rate: 0.018,
      reasons: [
        "Need 22 more completed executions before the validator can graduate the run.",
        "PredictIt inventory still requires operator-confirmed exits.",
      ],
    },
  };
}

function jsonResponse(route, data, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(data),
  });
}

async function stubAtlasApis(page, payloads) {
  await page.route("**/api/**", async (route) => {
    const { pathname } = new URL(route.request().url());

    if (pathname === "/api/system") return jsonResponse(route, payloads.system);
    if (pathname === "/api/opportunities") return jsonResponse(route, payloads.opportunities);
    if (pathname === "/api/trades") return jsonResponse(route, payloads.trades);
    if (pathname === "/api/errors") return jsonResponse(route, payloads.errors);
    if (pathname === "/api/manual-positions") return jsonResponse(route, payloads["manual-positions"]);
    if (pathname === "/api/market-mappings") return jsonResponse(route, payloads["market-mappings"]);
    if (pathname === "/api/portfolio") return jsonResponse(route, payloads.portfolio);
    if (pathname === "/api/profitability") return jsonResponse(route, payloads.profitability);
    if (pathname === "/api/auth/me") {
      const authenticated = route.request().headers().authorization === `Bearer ${AUTH_TOKEN}`;
      return jsonResponse(route, { authenticated, email: authenticated ? "operator@arbiter.local" : "" });
    }
    if (pathname === "/api/auth/login") {
      return jsonResponse(route, { token: AUTH_TOKEN, email: "operator@arbiter.local" });
    }
    if (pathname === "/api/auth/logout") {
      return jsonResponse(route, { ok: true });
    }

    return route.continue();
  });
}

async function runScenario(browser, name, contextOptions, url, options = {}) {
  const payloads = buildPayloads();
  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();
  const errors = [];

  page.on("console", (msg) => {
    if (msg.type() === "error" && !msg.text().includes("/ws")) errors.push(msg.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("requestfailed", (req) => {
    if (!req.url().includes("/ws")) errors.push(`requestfailed:${req.url()}`);
  });

  await stubAtlasApis(page, payloads);
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  if (options.auth) {
    await page.fill("#authEmail", "operator@arbiter.local");
    await page.fill("#authPassword", "secret");
    await page.click("#authSubmit");
    await page.waitForFunction(() => document.querySelector("#authOverlay")?.classList.contains("hidden"));
    await page.waitForTimeout(300);
  }

  await page.locator("#logsSection").scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);

  if (options.presentationMode) {
    await page.click(`[data-log-presentation="${options.presentationMode}"]`);
    await page.waitForTimeout(250);
  }

  const facts = await page.evaluate(() => {
    const timeline = document.querySelector("#logTimeline");
    const consoleEl = document.querySelector(".log-console");
    const modeButtons = [...document.querySelectorAll("[data-log-presentation]")].map((button) => ({
      label: button.textContent.replace(/\s+/g, " ").trim(),
      key: button.getAttribute("data-log-presentation"),
      pressed: button.getAttribute("aria-pressed") === "true",
      hidden: window.getComputedStyle(button).display === "none",
    }));
    const rows = [...document.querySelectorAll(".log-entry")].map((row) => ({
      digest: row.classList.contains("is-digest"),
      title: row.querySelector(".log-entry-title")?.textContent?.replace(/\s+/g, " ").trim() || "",
      meta: row.querySelector(".log-entry-meta")?.textContent?.replace(/\s+/g, " ").trim() || "",
      tagCount: row.querySelectorAll(".log-entry-tags span").length,
      clientWidth: row.clientWidth,
      scrollWidth: row.scrollWidth,
      clientHeight: row.clientHeight,
      scrollHeight: row.scrollHeight,
    }));

    return {
      hasHorizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      timelineClientHeight: timeline ? Math.round(timeline.clientHeight) : 0,
      timelineScrollHeight: timeline ? Math.round(timeline.scrollHeight) : 0,
      consoleHeight: consoleEl ? Math.round(consoleEl.getBoundingClientRect().height) : 0,
      pageHeight: Math.round(document.documentElement.scrollHeight),
      modeButtons,
      rows,
      digestRows: rows.filter((row) => row.digest).length,
      compactRows: rows.filter((row) => row.title && row.meta && row.tagCount > 0).length,
      summaryText: document.querySelector("#logResultSummary")?.textContent?.replace(/\s+/g, " ").trim() || "",
    };
  });

  const screenshot = `output/playwright/${name}-activity-atlas.png`;
  await page.screenshot({ path: screenshot, fullPage: false });
  await context.close();

  return {
    name,
    screenshot,
    errors,
    facts,
  };
}

const browser = await chromium.launch({ headless: true });
const results = [];
results.push(await runScenario(
  browser,
  "desktop-atlas-digest",
  { viewport: { width: 1440, height: 1180 } },
  "http://127.0.0.1:8090/",
  { presentationMode: "digest" },
));
results.push(await runScenario(
  browser,
  "desktop-atlas-stream",
  { viewport: { width: 1440, height: 1180 } },
  "http://127.0.0.1:8090/",
  { presentationMode: "stream" },
));
results.push(await runScenario(
  browser,
  "mobile-atlas",
  devices["iPhone 13"],
  "http://127.0.0.1:8090/",
));
await browser.close();

fs.mkdirSync("output/playwright", { recursive: true });
fs.writeFileSync("output/playwright/activity-atlas-check.json", JSON.stringify(results, null, 2));

const failures = [];
for (const result of results) {
  const { name, errors, facts } = result;
  if (errors.length) failures.push(`${name}: console/page errors detected: ${errors.join(" | ")}`);
  if (facts.hasHorizontalOverflow) failures.push(`${name}: document overflows horizontally.`);

  for (const row of facts.rows) {
    if (!row.title || !row.meta || row.tagCount < 1) {
      failures.push(`${name}: atlas row is missing compact title/meta/tags.`);
      break;
    }
    if (row.scrollWidth > row.clientWidth + 1 || row.scrollHeight > row.clientHeight + 1) {
      failures.push(`${name}: atlas row overflows its compact card bounds (${row.title}).`);
      break;
    }
  }

  if (name === "desktop-atlas-digest") {
    const digestButton = facts.modeButtons.find((button) => button.key === "digest");
    if (!digestButton || digestButton.hidden) failures.push(`${name}: digest control is missing on desktop.`);
    if (!digestButton?.pressed) failures.push(`${name}: digest control did not stay active after selection.`);
    if (facts.digestRows < 1) failures.push(`${name}: digest mode did not render any grouped digest rows.`);
    if (!/digest/i.test(facts.summaryText)) failures.push(`${name}: digest summary copy did not update.`);
    if (!(facts.timelineScrollHeight > facts.timelineClientHeight && facts.timelineClientHeight > 0)) {
      failures.push(`${name}: timeline did not stay internally scrollable in digest mode.`);
    }
  }

  if (name === "desktop-atlas-stream") {
    const streamButton = facts.modeButtons.find((button) => button.key === "stream");
    if (!streamButton || streamButton.hidden) failures.push(`${name}: stream control is missing on desktop.`);
    if (!streamButton?.pressed) failures.push(`${name}: stream control did not stay active after selection.`);
    if (facts.digestRows !== 0) failures.push(`${name}: stream mode still rendered digest rows.`);
  }

  if (name === "mobile-atlas") {
    const visibleButtons = facts.modeButtons.filter((button) => !button.hidden);
    if (visibleButtons.length) failures.push(`${name}: desktop-only digest controls are visible on mobile.`);
  }
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}

console.log("Activity atlas checks passed.");
