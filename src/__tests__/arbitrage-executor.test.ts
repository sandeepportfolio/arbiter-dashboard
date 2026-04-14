import { describe, it, expect, beforeEach } from "vitest";
import { ArbitrageExecutor } from "../execution/arbitrage-executor.js";
import { RiskGate } from "../execution/risk-gate.js";
import { TradeLogger } from "../execution/trade-logger.js";
import type { ArbitrageOpportunity, MatchedEvent, PricePoint } from "../types.js";

function makeOpportunity(overrides: Partial<ArbitrageOpportunity> = {}): ArbitrageOpportunity {
  const prices = new Map<string, PricePoint>();
  const matchedEvent: MatchedEvent = {
    eventKey: "test-event",
    title: "Will BTC hit 100k?",
    contracts: [
      {
        contractKey: "btc-100k-yes",
        title: "Yes",
        prices: prices as any,
      },
    ],
  };

  return {
    id: "opp-1",
    matchedEvent,
    contractKey: "btc-100k-yes",
    buyPlatform: "predictit",
    sellPlatform: "kalshi",
    buyPrice: 0.40,
    sellPrice: 0.55,
    spread: 0.15,
    expectedProfit: 0.10,
    confidence: 0.8,
    detectedAt: new Date("2026-04-14T00:00:00Z"),
    ...overrides,
  };
}

describe("ArbitrageExecutor", () => {
  let riskGate: RiskGate;
  let logger: TradeLogger;
  let executor: ArbitrageExecutor;

  beforeEach(() => {
    riskGate = new RiskGate({
      maxPositionSize: 50,
      maxExposure: 500,
      minSpread: 0.02,
    });
    logger = new TradeLogger("/tmp/test-trade-logs");
    executor = new ArbitrageExecutor(riskGate, logger, true);
  });

  it("executes a dry-run trade with both legs", async () => {
    const opp = makeOpportunity();
    const results = await executor.executeBatch([opp]);

    expect(results).toHaveLength(1);
    const result = results[0];

    expect(result.dryRun).toBe(true);
    expect(result.opportunity.id).toBe("opp-1");

    // Buy leg should be executed
    expect(result.buyLeg.status).toBe("executed");
    expect(result.buyLeg.buyPlatform).toBe("predictit");
    expect(result.buyLeg.buyPrice).toBe(0.40);
    expect(result.buyLeg.reason).toContain("DRY RUN");

    // Sell leg should be present for supported platforms
    expect(result.sellLeg).not.toBeNull();
    expect(result.sellLeg!.status).toBe("executed");
    expect(result.sellLeg!.sellPlatform).toBe("kalshi");
    expect(result.sellLeg!.reason).toContain("DRY RUN");
  });

  it("skips opportunities rejected by risk gate", async () => {
    const opp = makeOpportunity({
      spread: 0.001, // below minSpread of 0.02
      expectedProfit: 0.0005,
    });

    const results = await executor.executeBatch([opp]);

    expect(results).toHaveLength(1);
    const result = results[0];

    expect(result.buyLeg.status).toBe("skipped");
    expect(result.buyLeg.reason).toContain("Spread");
    expect(result.sellLeg).toBeNull();
  });

  it("skips opportunities with negative expected profit", async () => {
    const opp = makeOpportunity({
      expectedProfit: -0.05,
    });

    const results = await executor.executeBatch([opp]);
    expect(results[0].buyLeg.status).toBe("skipped");
    expect(results[0].buyLeg.reason).toContain("non-positive");
  });

  it("skips when exposure limit would be exceeded", async () => {
    const tightGate = new RiskGate({
      maxPositionSize: 50,
      maxExposure: 0.01, // very low
      minSpread: 0.02,
    });
    const tightExecutor = new ArbitrageExecutor(tightGate, logger, true);

    const opp = makeOpportunity();
    const results = await tightExecutor.executeBatch([opp]);

    expect(results[0].buyLeg.status).toBe("skipped");
    expect(results[0].buyLeg.reason).toContain("exposure");
  });

  it("handles multiple opportunities in batch", async () => {
    const opps = [
      makeOpportunity({ id: "opp-1" }),
      makeOpportunity({ id: "opp-2", buyPrice: 0.30, sellPrice: 0.50, spread: 0.20 }),
      makeOpportunity({ id: "opp-3", buyPrice: 0.45, sellPrice: 0.60, spread: 0.15 }),
    ];

    const results = await executor.executeBatch(opps);
    expect(results).toHaveLength(3);
    expect(results.map((r) => r.opportunity.id)).toEqual(["opp-1", "opp-2", "opp-3"]);
  });

  it("logs arbitrage entries via TradeLogger", async () => {
    const opp = makeOpportunity();
    await executor.executeBatch([opp]);

    const entries = logger.getArbitrageEntries();
    expect(entries).toHaveLength(1);
    expect(entries[0].type).toBe("arbitrage");
    expect(entries[0].matchedEvent).toBe("Will BTC hit 100k?");
    expect(entries[0].buyPlatform).toBe("predictit");
    expect(entries[0].sellPlatform).toBe("kalshi");
    expect(entries[0].dryRun).toBe(true);
  });

  it("computes fees and net edge correctly", async () => {
    const opp = makeOpportunity({
      buyPrice: 0.40,
      sellPrice: 0.55,
      spread: 0.15,
    });

    const results = await executor.executeBatch([opp]);
    const result = results[0];

    // Both legs should be executed
    expect(result.buyLeg.status).toBe("executed");
    expect(result.sellLeg).not.toBeNull();

    // Net profit should be positive (spread > fees)
    const quantity = result.buyLeg.quantity;
    expect(quantity).toBeGreaterThan(0);

    // Gross profit = spread * quantity
    const expectedGross = 0.15 * quantity;
    expect(result.buyLeg.grossProfit).toBeCloseTo(expectedGross, 6);

    // Fees = 5% on each leg
    const buyFees = 0.40 * 0.05 * quantity;
    const sellFees = 0.55 * 0.05 * quantity;
    expect(result.buyLeg.fees).toBeCloseTo(buyFees, 6);
    expect(result.sellLeg!.fees).toBeCloseTo(sellFees, 6);

    // Net = gross - total fees
    const totalFees = buyFees + sellFees;
    expect(result.buyLeg.netProfit).toBeCloseTo(expectedGross - totalFees, 6);
  });

  it("sets sellLeg to null for unsupported sell platforms", async () => {
    const opp = makeOpportunity({
      sellPlatform: "some_unknown_exchange" as any,
    });

    const results = await executor.executeBatch([opp]);
    expect(results[0].sellLeg).toBeNull();
    // Buy leg should still execute
    expect(results[0].buyLeg.status).toBe("executed");
  });

  it("sets dryRun flag consistently across result", async () => {
    const opp = makeOpportunity();
    const results = await executor.executeBatch([opp]);
    const result = results[0];

    expect(result.dryRun).toBe(true);
    expect(result.buyLeg.dryRun).toBe(true);
    if (result.sellLeg) {
      expect(result.sellLeg.dryRun).toBe(true);
    }
  });
});
