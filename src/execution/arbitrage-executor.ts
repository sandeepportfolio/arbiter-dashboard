/**
 * Arbitrage executor — STUB
 * Executes arbitrage trades (dry-run mode).
 *
 * Full implementation will come from ARB-131 (Trade Executor).
 * This stub logs what would be traded without placing real orders.
 */

import type { ArbitrageOpportunity, TradeResult, RiskCheckResult } from "../types.js";
import { RiskGate } from "./risk-gate.js";
import { TradeLogger } from "./trade-logger.js";

export class ArbitrageExecutor {
  constructor(
    private riskGate: RiskGate,
    private logger: TradeLogger,
    private dryRun: boolean = true,
  ) {}

  async executeBatch(opportunities: ArbitrageOpportunity[]): Promise<TradeResult[]> {
    const results: TradeResult[] = [];

    for (const opp of opportunities) {
      const riskCheck = this.riskGate.check(opp);

      if (!riskCheck.approved) {
        const result: TradeResult = {
          opportunityId: opp.id,
          buyPlatform: opp.buyPlatform,
          sellPlatform: opp.sellPlatform,
          buyPrice: opp.buyPrice,
          sellPrice: opp.sellPrice,
          quantity: 0,
          grossProfit: 0,
          fees: 0,
          netProfit: 0,
          dryRun: this.dryRun,
          executedAt: new Date(),
          status: "skipped",
          reason: riskCheck.reason,
        };
        results.push(result);
        await this.logger.log(result);
        continue;
      }

      const quantity = riskCheck.adjustedQuantity ?? 1;
      const grossProfit = opp.spread * quantity;
      const fees = (opp.buyPrice * 0.05 + opp.sellPrice * 0.05) * quantity; // rough fee estimate
      const netProfit = grossProfit - fees;

      const result: TradeResult = {
        opportunityId: opp.id,
        buyPlatform: opp.buyPlatform,
        sellPlatform: opp.sellPlatform,
        buyPrice: opp.buyPrice,
        sellPrice: opp.sellPrice,
        quantity,
        grossProfit,
        fees,
        netProfit,
        dryRun: this.dryRun,
        executedAt: new Date(),
        status: this.dryRun ? "executed" : "executed",
      };

      if (this.dryRun) {
        result.reason = "DRY RUN — no real orders placed";
      }

      results.push(result);
      await this.logger.log(result);
    }

    return results;
  }
}
