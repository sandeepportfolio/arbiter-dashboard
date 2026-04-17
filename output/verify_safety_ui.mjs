#!/usr/bin/env node
/**
 * verify_safety_ui.mjs — Phase 3 (Safety Layer) operator smoke test
 *
 * Confirms the dashboard loads without console errors and every safety-layer
 * DOM node introduced by plan 03-07 is present, while the existing Phase 1/2
 * panels continue to render.
 *
 * Usage:
 *   node output/verify_safety_ui.mjs [--dry-run] [--url=http://localhost:8080]
 *
 * --dry-run      Print the usage banner and exit without importing Playwright.
 *                Useful for CI smoke-acceptance when the dashboard is not
 *                running.
 * --url=<base>   Override the base URL. Default: http://localhost:8080.
 *
 * The script writes a JSON summary to output/verify_safety_ui.json:
 *   { consoleErrors: string[], missingSelectors: string[], passed: boolean }
 */

import { writeFileSync } from "node:fs";

const args = process.argv.slice(2);

if (args.includes("--dry-run")) {
  console.log(
    "verify_safety_ui — Phase 3 smoke. Usage: node output/verify_safety_ui.mjs [--dry-run] [--url=http://localhost:8080]",
  );
  console.log("Checks: dashboard.html + index.html safety markup, console errors, existing panels intact.");
  process.exit(0);
}

// Only now import playwright — keeps --dry-run fast and free of dev deps.
const { chromium } = await import("playwright");

function parseUrlArg(flag) {
  const arg = args.find((value) => value.startsWith(`${flag}=`));
  return arg ? arg.slice(flag.length + 1) : null;
}

const baseUrl = parseUrlArg("--url") || "http://localhost:8080";

// Selectors introduced (or kept) by plan 03-07. Each is expected to exist in
// the DOM after the dashboard finishes its first render/settle.
const SAFETY_SELECTORS = [
  "#safetySection",
  "#killSwitchBadge",
  "#killSwitchArm",
  "#killSwitchReset",
  "#killSwitchCooldown",
  "#rateLimitIndicators",
  "#oneLegAlertPanel",
  "#oneLegAlertBody",
  "#shutdownBanner",
  "#shutdownBannerText",
];

// Pre-existing panel anchors from earlier phases. These must still be present
// to satisfy the "no regression" acceptance criterion.
const EXISTING_SELECTORS = [
  "#overviewSection",
  "#commandCenter",
  "#opportunitiesSection",
  "#opportunityList",
  "#riskSection",
  "#portfolioList",
  "#opsSection",
  "#incidentList",
  "#manualQueue",
  "#logsSection",
  "#logTimeline",
  "#infraSection",
  "#mappingList",
  "#collectorList",
];

async function verifyPage(browser, url) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      consoleErrors.push(`${msg.location().url || "?"}: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => {
    consoleErrors.push(`pageerror: ${err.message}`);
  });

  const response = await page.goto(url, { waitUntil: "domcontentloaded" });
  if (!response || !response.ok()) {
    await context.close();
    return {
      url,
      status: response ? response.status() : 0,
      consoleErrors,
      missingSelectors: [...SAFETY_SELECTORS, ...EXISTING_SELECTORS],
      reachable: false,
    };
  }

  // Give the dashboard time to bootstrap + render.
  await page.waitForTimeout(3000);

  const missingSelectors = [];
  for (const selector of [...SAFETY_SELECTORS, ...EXISTING_SELECTORS]) {
    const node = await page.$(selector);
    if (!node) missingSelectors.push(selector);
  }

  await context.close();
  return {
    url,
    status: response.status(),
    consoleErrors,
    missingSelectors,
    reachable: true,
  };
}

const browser = await chromium.launch({ headless: true });
const results = [];
const urlsToCheck = [`${baseUrl}/index.html`];
// When the static-frontend variant is served alongside the aiohttp app (some
// deploys expose / as the static index.html while /index.html serves the
// backend-rendered dashboard.html) the smoke can also verify the root URL.
// We attempt it opportunistically — failure to reach it is not fatal when a
// single URL already passed.
urlsToCheck.push(`${baseUrl}/`);

for (const url of urlsToCheck) {
  try {
    results.push(await verifyPage(browser, url));
  } catch (err) {
    results.push({
      url,
      status: 0,
      consoleErrors: [`navigation failed: ${err instanceof Error ? err.message : String(err)}`],
      missingSelectors: [...SAFETY_SELECTORS, ...EXISTING_SELECTORS],
      reachable: false,
    });
  }
}
await browser.close();

const aggregatedErrors = [];
const aggregatedMissing = new Set();
let anyReachable = false;
for (const result of results) {
  if (result.reachable) anyReachable = true;
  for (const err of result.consoleErrors) aggregatedErrors.push(`[${result.url}] ${err}`);
  for (const missing of result.missingSelectors) {
    // Only count missing selectors from URLs we were able to reach — a 404 on
    // one variant should not poison the summary when the other variant served
    // a full dashboard.
    if (result.reachable) aggregatedMissing.add(missing);
  }
}

const passed = anyReachable && aggregatedErrors.length === 0 && aggregatedMissing.size === 0;

const summary = {
  baseUrl,
  urlsChecked: results.map((r) => ({ url: r.url, status: r.status, reachable: r.reachable })),
  consoleErrors: aggregatedErrors,
  missingSelectors: [...aggregatedMissing],
  passed,
};

try {
  writeFileSync("output/verify_safety_ui.json", JSON.stringify(summary, null, 2));
} catch (err) {
  console.error("Could not write output/verify_safety_ui.json:", err);
}

console.log(JSON.stringify(summary, null, 2));
if (!passed) process.exit(1);
