/**
 * Arbitrage detector — STUB
 * Identifies pricing discrepancies across matched events.
 *
 * Full implementation will come from ARB-130 (Market Scanner).
 * This stub provides the interface and a simple single-platform fair-value check.
 */

import type { MatchedEvent, ArbitrageOpportunity } from "../types.js";

export interface DetectorConfig {
  minSpread: number; // minimum spread to report (default 0.02 = 2%)
  fees: Record<string, number>; // platform fee rates
}

const DEFAULT_FEES: Record<string, number> = {
  kalshi: 0.01, // ~1% fee estimate
  polymarket: 0.02,
};

export class ArbitrageDetector {
  private readonly config: DetectorConfig;

  constructor(config?: Partial<DetectorConfig>) {
    this.config = {
      minSpread: config?.minSpread ?? 0.02,
      fees: config?.fees ?? DEFAULT_FEES,
    };
  }

  /**
   * Detect arbitrage opportunities from matched events.
   *
   * Stub: only detects opportunities where a matched event has prices
   * from 2+ platforms. Since the EventMatcher stub doesn't cross-match yet,
   * this will return empty until ARB-130 is implemented.
   */
  detect(matchedEvents: MatchedEvent[]): ArbitrageOpportunity[] {
    const opportunities: ArbitrageOpportunity[] = [];

    for (const event of matchedEvents) {
      for (const contract of event.contracts) {
        // Need prices from at least 2 platforms to find arbitrage
        if (contract.prices.size < 2) continue;

        const entries = Array.from(contract.prices.entries());

        // Compare all platform pairs
        for (let i = 0; i < entries.length; i++) {
          for (let j = i + 1; j < entries.length; j++) {
            const [platA, priceA] = entries[i];
            const [platB, priceB] = entries[j];

            // Check if buying YES on A and NO on B (or vice versa) is profitable
            // Arb exists when: yesPrice_A + yesPrice_B < 1 (same event, opposite positions)
            // Or: yesPrice_A < (1 - yesPrice_B) after fees

            const spreadAB = priceB.yesPrice - priceA.yesPrice;
            const spreadBA = priceA.yesPrice - priceB.yesPrice;

            if (Math.abs(spreadAB) >= this.config.minSpread) {
              const buyPlat = spreadAB > 0 ? platA : platB;
              const sellPlat = spreadAB > 0 ? platB : platA;
              const buyPrice = spreadAB > 0 ? priceA.yesPrice : priceB.yesPrice;
              const sellPrice = spreadAB > 0 ? priceB.yesPrice : priceA.yesPrice;
              const spread = sellPrice - buyPrice;
              const buyFee = this.config.fees[buyPlat] ?? 0.02;
              const sellFee = this.config.fees[sellPlat] ?? 0.02;
              const expectedProfit = spread - (buyPrice * buyFee) - (sellPrice * sellFee);

              if (expectedProfit > 0) {
                opportunities.push({
                  id: `arb-${event.eventKey}-${contract.contractKey}-${Date.now()}`,
                  matchedEvent: event,
                  contractKey: contract.contractKey,
                  buyPlatform: buyPlat,
                  sellPlatform: sellPlat,
                  buyPrice,
                  sellPrice,
                  spread,
                  expectedProfit,
                  confidence: 0.5, // stub confidence
                  detectedAt: new Date(),
                });
              }
            }
          }
        }
      }
    }

    return opportunities.sort((a, b) => b.expectedProfit - a.expectedProfit);
  }
}
