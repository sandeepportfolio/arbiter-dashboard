/**
 * Risk gate — validates opportunities before execution.
 */

import type { ArbitrageOpportunity, RiskCheckResult, PipelineConfig } from "../types.js";

export class RiskGate {
  private totalExposure = 0;

  constructor(private config: Pick<PipelineConfig, "maxPositionSize" | "maxExposure" | "minSpread">) {}

  check(opportunity: ArbitrageOpportunity): RiskCheckResult {
    // Check minimum spread
    if (opportunity.spread < this.config.minSpread) {
      return { approved: false, reason: `Spread ${(opportunity.spread * 100).toFixed(2)}% below minimum ${(this.config.minSpread * 100).toFixed(2)}%` };
    }

    // Check expected profitability
    if (opportunity.expectedProfit <= 0) {
      return { approved: false, reason: "Expected profit is non-positive after fees" };
    }

    // Check exposure limit
    const tradeSize = Math.min(this.config.maxPositionSize, opportunity.buyPrice * 100); // $100 notional per contract
    if (this.totalExposure + tradeSize > this.config.maxExposure) {
      return { approved: false, reason: `Would exceed max exposure ($${this.config.maxExposure})` };
    }

    // Check confidence
    if (opportunity.confidence < 0.3) {
      return { approved: false, reason: `Confidence ${opportunity.confidence} below threshold 0.3` };
    }

    this.totalExposure += tradeSize;
    return {
      approved: true,
      reason: "Passed all risk checks",
      adjustedQuantity: Math.floor(tradeSize / opportunity.buyPrice),
    };
  }

  reset(): void {
    this.totalExposure = 0;
  }
}
