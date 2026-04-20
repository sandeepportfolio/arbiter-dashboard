/**
 * Arbitrage executor — executes dry-run trades for cross-platform arbitrage.
 *
 * For each ArbitrageOpportunity, creates both a buy leg (buy YES on platform A)
 * and a sell leg (sell YES / buy NO on platform B), validates through the risk
 * gate, and logs both legs via TradeLogger.
 */

import type {
  ArbitrageOpportunity,
  ArbitrageTradeResult,
  TradeResult,
  RiskCheckResult,
} from "../types.js";
import { RiskGate } from "./risk-gate.js";
import { TradeLogger } from "./trade-logger.js";

/** Platforms where sell-side execution is supported in dry-run mode. */
const SUPPORTED_SELL_PLATFORMS = new Set(["kalshi", "polymarket"]);

export class ArbitrageExecutor {
  constructor(
    private riskGate: RiskGate,
    private logger: TradeLogger,
    private dryRun: boolean = true,
  ) {}

  async executeBatch(
    opportunities: ArbitrageOpportunity[],
  ): Promise<ArbitrageTradeResult[]> {
    const results: ArbitrageTradeResult[] = [];

    for (const opp of opportunities) {
      const result = await this.executeOne(opp);
      results.push(result);
    }

    return results;
  }

  private async executeOne(
    opp: ArbitrageOpportunity,
  ): Promise<ArbitrageTradeResult> {
    const riskCheck = this.riskGate.check(opp);

    if (!riskCheck.approved) {
      return this.buildSkippedResult(opp, riskCheck);
    }

    const quantity = riskCheck.adjustedQuantity ?? 1;
    const feeRate = 0.05; // 5% per leg (rough estimate)

    // Buy leg — buy YES on the cheaper platform
    const buyFees = opp.buyPrice * feeRate * quantity;
    const buyLeg: TradeResult = {
      opportunityId: opp.id,
      buyPlatform: opp.buyPlatform,
      sellPlatform: opp.sellPlatform,
      buyPrice: opp.buyPrice,
      sellPrice: opp.sellPrice,
      quantity,
      grossProfit: 0, // profit is realized on the pair, not a single leg
      fees: buyFees,
      netProfit: -buyFees,
      dryRun: this.dryRun,
      executedAt: new Date(),
      status: "executed",
      reason: this.dryRun ? "DRY RUN — buy leg" : undefined,
    };

    // Sell leg — sell YES (or buy NO) on the more expensive platform
    const sellSupported = SUPPORTED_SELL_PLATFORMS.has(opp.sellPlatform);
    let sellLeg: TradeResult | null = null;

    if (sellSupported) {
      const sellFees = opp.sellPrice * feeRate * quantity;
      const grossProfit = opp.spread * quantity;
      const totalFees = buyFees + sellFees;

      sellLeg = {
        opportunityId: opp.id,
        buyPlatform: opp.buyPlatform,
        sellPlatform: opp.sellPlatform,
        buyPrice: opp.buyPrice,
        sellPrice: opp.sellPrice,
        quantity,
        grossProfit,
        fees: sellFees,
        netProfit: grossProfit - totalFees,
        dryRun: this.dryRun,
        executedAt: new Date(),
        status: "executed",
        reason: this.dryRun ? "DRY RUN — sell leg" : undefined,
      };

      // Update buy leg to reflect the combined profit
      buyLeg.grossProfit = grossProfit;
      buyLeg.netProfit = grossProfit - totalFees;
    }

    // Log both legs
    await this.logger.logArbitrageTrade(opp, buyLeg, sellLeg, this.dryRun);

    return {
      opportunity: opp,
      buyLeg,
      sellLeg,
      dryRun: this.dryRun,
      timestamp: new Date(),
    };
  }

  private buildSkippedResult(
    opp: ArbitrageOpportunity,
    riskCheck: RiskCheckResult,
  ): ArbitrageTradeResult {
    const skippedLeg: TradeResult = {
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

    // Log the skip
    this.logger.log(skippedLeg);

    return {
      opportunity: opp,
      buyLeg: skippedLeg,
      sellLeg: null,
      dryRun: this.dryRun,
      timestamp: new Date(),
    };
  }
}
