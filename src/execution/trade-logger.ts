/**
 * Trade logger — writes trade results to trade-logs/ directory.
 */

import { writeFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import type { TradeResult } from "../types.js";

export class TradeLogger {
  private readonly logDir: string;
  private results: TradeResult[] = [];

  constructor(logDir: string = "trade-logs") {
    this.logDir = logDir;
  }

  async log(result: TradeResult): Promise<void> {
    this.results.push(result);
  }

  async flush(): Promise<string> {
    await mkdir(this.logDir, { recursive: true });

    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = `dry-run-${timestamp}.json`;
    const filepath = join(this.logDir, filename);

    const logData = {
      runTimestamp: new Date().toISOString(),
      dryRun: true,
      totalTrades: this.results.length,
      executed: this.results.filter((r) => r.status === "executed").length,
      skipped: this.results.filter((r) => r.status === "skipped").length,
      failed: this.results.filter((r) => r.status === "failed").length,
      totalNetProfit: this.results.reduce((sum, r) => sum + r.netProfit, 0),
      trades: this.results,
    };

    await writeFile(filepath, JSON.stringify(logData, null, 2));
    return filepath;
  }

  getResults(): TradeResult[] {
    return [...this.results];
  }
}
