#!/usr/bin/env node
/**
 * Arbiter CLI — main orchestrator for the arbitrage pipeline.
 *
 * Pipeline: Collect prices → Match events → Detect arbitrage → Risk gate → Execute (dry-run) → Log
 *
 * Usage:
 *   npx tsx src/cli.ts
 *   node dist/cli.js
 *
 * Environment:
 *   KALSHI_API_KEY          — Kalshi API key (optional, skips Kalshi if missing)
 *   KALSHI_PRIVATE_KEY_PATH — Path to Kalshi RSA private key (optional)
 */

import { PredictItClient, PredictItCollector } from "./collectors/predictit-client.js";
import { KalshiClient, KalshiCollector } from "./collectors/kalshi-client.js";
import { EventMatcher } from "./matching/event-matcher.js";
import { ArbitrageDetector } from "./matching/arbitrage-detector.js";
import { RiskGate } from "./execution/risk-gate.js";
import { ArbitrageExecutor } from "./execution/arbitrage-executor.js";
import { TradeLogger } from "./execution/trade-logger.js";
import type { PipelineConfig, PricePoint } from "./types.js";

function loadConfig(): PipelineConfig {
  return {
    kalshiApiKey: process.env.KALSHI_API_KEY,
    kalshiPrivateKeyPath: process.env.KALSHI_PRIVATE_KEY_PATH,
    dryRun: true,
    maxPositionSize: 50, // $50 max per position
    minSpread: 0.02, // 2% minimum spread
    maxExposure: 500, // $500 max total exposure
  };
}

function printHeader(): void {
  console.log("═══════════════════════════════════════════════════════");
  console.log("  ARBITER — Cross-Platform Prediction Market Arbitrage");
  console.log("  Mode: DRY RUN");
  console.log(`  Time: ${new Date().toISOString()}`);
  console.log("═══════════════════════════════════════════════════════");
  console.log();
}

function printTable(rows: string[][]): void {
  if (rows.length === 0) return;
  const widths = rows[0].map((_, colIdx) =>
    Math.max(...rows.map((row) => (row[colIdx] ?? "").length))
  );
  for (const row of rows) {
    console.log(
      "  " + row.map((cell, i) => cell.padEnd(widths[i])).join("  ")
    );
  }
}

async function main(): Promise<void> {
  printHeader();

  const config = loadConfig();

  // Step 1: Initialize collectors
  console.log("[1/6] Initializing collectors...");
  const predictitClient = new PredictItClient();
  const predictitCollector = new PredictItCollector(predictitClient);

  const kalshiClient = new KalshiClient({
    apiKey: config.kalshiApiKey,
    privateKeyPath: config.kalshiPrivateKeyPath,
    dryRun: config.dryRun,
  });
  const kalshiCollector = new KalshiCollector(kalshiClient);

  const activePlatforms: string[] = [];
  if (kalshiClient.isAvailable()) activePlatforms.push("Kalshi");
  activePlatforms.push("PredictIt"); // always available (no auth)

  console.log(`  Active platforms: ${activePlatforms.join(", ")}`);
  console.log();

  // Step 2: Collect prices
  console.log("[2/6] Fetching market data...");
  const allPrices: PricePoint[][] = [];

  try {
    const predictitPrices = await predictitCollector.collect();
    allPrices.push(predictitPrices);
    console.log(`  PredictIt: ${predictitPrices.length} contracts fetched`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`  PredictIt: FAILED — ${msg}`);
  }

  try {
    const kalshiPrices = await kalshiCollector.collect();
    if (kalshiPrices.length > 0) {
      allPrices.push(kalshiPrices);
      console.log(`  Kalshi: ${kalshiPrices.length} contracts fetched`);
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`  Kalshi: FAILED — ${msg}`);
  }

  const totalPrices = allPrices.reduce((sum, p) => sum + p.length, 0);
  if (totalPrices === 0) {
    console.error("\n  ERROR: No market data available from any platform. Exiting.");
    process.exit(1);
  }
  console.log();

  // Step 3: Match events across platforms
  console.log("[3/6] Matching events across platforms...");
  const matcher = new EventMatcher();
  const matchedEvents = matcher.match(...allPrices);
  console.log(`  Matched events: ${matchedEvents.length}`);
  console.log(`  Cross-platform matches: ${matcher.crossPlatformMatchCount}`);
  if (matcher.crossPlatformMatchCount === 0) {
    console.log("  Note: Cross-platform matching is stubbed — waiting for ARB-130 implementation");
  }
  console.log();

  // Step 4: Detect arbitrage opportunities
  console.log("[4/6] Scanning for arbitrage opportunities...");
  const detector = new ArbitrageDetector({ minSpread: config.minSpread });
  const opportunities = detector.detect(matchedEvents);
  console.log(`  Opportunities found: ${opportunities.length}`);
  if (opportunities.length > 0) {
    console.log();
    console.log("  Top opportunities:");
    const header = ["Rank", "Event", "Buy@", "Sell@", "Spread", "Est.Profit"];
    const rows = [header];
    for (const [idx, opp] of opportunities.slice(0, 10).entries()) {
      rows.push([
        `${idx + 1}`,
        opp.matchedEvent.title.slice(0, 40),
        `${opp.buyPlatform}@${opp.buyPrice.toFixed(2)}`,
        `${opp.sellPlatform}@${opp.sellPrice.toFixed(2)}`,
        `${(opp.spread * 100).toFixed(1)}%`,
        `$${opp.expectedProfit.toFixed(2)}`,
      ]);
    }
    printTable(rows);
  } else {
    console.log("  Note: No cross-platform opportunities yet (event matcher is stubbed)");
  }
  console.log();

  // Step 5: Execute dry-run trades
  console.log("[5/6] Executing dry-run trades...");
  const logger = new TradeLogger("trade-logs");
  const riskGate = new RiskGate({
    maxPositionSize: config.maxPositionSize,
    maxExposure: config.maxExposure,
    minSpread: config.minSpread,
  });
  const executor = new ArbitrageExecutor(riskGate, logger, config.dryRun);
  const arbResults = await executor.executeBatch(opportunities);

  const executed = arbResults.filter((r) => r.buyLeg.status === "executed");
  const skipped = arbResults.filter((r) => r.buyLeg.status === "skipped");
  console.log(`  Executed: ${executed.length}`);
  console.log(`  Skipped (risk gate): ${skipped.length}`);
  const withSellLeg = executed.filter((r) => r.sellLeg !== null);
  console.log(`  With sell leg: ${withSellLeg.length}`);
  console.log();

  // Step 6: Write trade log
  console.log("[6/6] Writing trade log...");
  const logPath = await logger.flush();
  console.log(`  Log written: ${logPath}`);
  console.log();

  // Summary
  console.log("═══════════════════════════════════════════════════════");
  console.log("  PIPELINE COMPLETE");
  console.log("═══════════════════════════════════════════════════════");
  console.log(`  Markets scanned:    ${totalPrices}`);
  console.log(`  Events matched:     ${matchedEvents.length}`);
  console.log(`  Cross-platform:     ${matcher.crossPlatformMatchCount}`);
  console.log(`  Opportunities:      ${opportunities.length}`);
  console.log(`  Dry-run trades:     ${executed.length} (${withSellLeg.length} with both legs)`);
  console.log(`  Trade log:          ${logPath}`);
  if (opportunities.length === 0) {
    console.log();
    console.log("  Status: Pipeline runs clean. No arbitrage detected.");
    console.log("  Next: Implement cross-platform event matching (ARB-130)");
    console.log("         to enable real arbitrage detection.");
  }
  console.log("═══════════════════════════════════════════════════════");
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
