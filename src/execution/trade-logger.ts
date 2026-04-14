/**
 * Trade logger — writes trade results to trade-logs/ directory.
 * Supports both single-leg TradeResult and dual-leg ArbitrageOpportunity logging.
 */

import { writeFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import type { ArbitrageOpportunity, TradeResult } from "../types.js";

export interface ArbitrageLogEntry {
  type: "arbitrage";
  opportunityId: string;
  matchedEvent: string;
  contractKey: string;
  buyPlatform: string;
  buyPrice: number;
  sellPlatform: string;
  sellPrice: number;
  spread: number;
  netEdge: number;
  dryRun: boolean;
  buyLeg: TradeResult;
  sellLeg: TradeResult | null;
  timestamp: string;
}

export class TradeLogger {
  private readonly logDir: string;
  private results: TradeResult[] = [];
  private arbitrageEntries: ArbitrageLogEntry[] = [];

  constructor(logDir: string = "trade-logs") {
    this.logDir = logDir;
  }

  async log(result: TradeResult): Promise<void> {
    this.results.push(result);
  }

  async logArbitrageTrade(
    opportunity: ArbitrageOpportunity,
    buyLeg: TradeResult,
    sellLeg: TradeResult | null,
    dryRun: boolean,
  ): Promise<void> {
    // Also push both legs into the flat results list for backwards compat
    this.results.push(buyLeg);
    if (sellLeg) this.results.push(sellLeg);

    const netEdge = sellLeg ? sellLeg.netProfit : 0;

    this.arbitrageEntries.push({
      type: "arbitrage",
      opportunityId: opportunity.id,
      matchedEvent: opportunity.matchedEvent.title,
      contractKey: opportunity.contractKey,
      buyPlatform: opportunity.buyPlatform,
      buyPrice: opportunity.buyPrice,
      sellPlatform: opportunity.sellPlatform,
      sellPrice: opportunity.sellPrice,
      spread: opportunity.spread,
      netEdge,
      dryRun,
      buyLeg,
      sellLeg,
      timestamp: new Date().toISOString(),
    });
  }

  async flush(): Promise<string> {
    await mkdir(this.logDir, { recursive: true });

    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = `dry-run-${timestamp}.json`;
    const filepath = join(this.logDir, filename);

    const executed = this.results.filter((r) => r.status === "executed");
    const skipped = this.results.filter((r) => r.status === "skipped");
    const failed = this.results.filter((r) => r.status === "failed");

    const logData = {
      runTimestamp: new Date().toISOString(),
      dryRun: true,
      totalTrades: this.results.length,
      executed: executed.length,
      skipped: skipped.length,
      failed: failed.length,
      totalNetProfit: this.results.reduce((sum, r) => sum + r.netProfit, 0),
      arbitrageTradeCount: this.arbitrageEntries.length,
      arbitrageTrades: this.arbitrageEntries,
      trades: this.results,
    };

    await writeFile(filepath, JSON.stringify(logData, null, 2));
    return filepath;
  }

  getResults(): TradeResult[] {
    return [...this.results];
  }

  getArbitrageEntries(): ArbitrageLogEntry[] {
    return [...this.arbitrageEntries];
  }
}
