import { describe, expect, it } from "vitest";
import { buildDeskOverview, buildMetricCards, buildOpportunityRows } from "./dashboard-view-model.js";

function makeState(overrides = {}) {
  return {
    system: {
      timestamp: 1_713_263_600,
      mode: "live",
      scanner: {
        tradable_opportunities: 7,
        active_opportunities: 14,
        best_edge_cents: 18.4,
        persistence_scans: 3,
        max_quote_age_seconds: 15,
      },
      execution: {
        total_pnl: 1_859.48,
        total_executions: 128,
        audit: { pass_rate: 0.982 },
      },
      audit: {
        audits_run: 2_048,
        pass_rate: 0.982,
      },
      balances: {
        kalshi: { balance: 1_200, is_low: false },
        predictit: { balance: 45, is_low: true },
      },
      collectors: {
        kalshi: { rate_limiter: { remaining_penalty_seconds: 0 } },
        polymarket: { rate_limiter: { remaining_penalty_seconds: 12 } },
      },
      series: {
        equity: [
          { timestamp: 1_713_260_000, equity: 40_100 },
          { timestamp: 1_713_263_600, equity: 42_150 },
        ],
      },
      counts: {
        prices: 5_200,
        incidents: 2,
      },
    },
    portfolio: {
      total_exposure: 21_400,
      total_open_positions: 12,
      violations: [{ level: "warning", message: "Concentration on election series" }],
      by_venue: {
        kalshi: { platform: "kalshi", total_exposure: 12_400, position_count: 7, is_low_balance: false },
        polymarket: { platform: "polymarket", total_exposure: 9_000, position_count: 5, is_low_balance: false },
      },
    },
    profitability: {
      verdict: "collecting_evidence",
      progress: 0.68,
      completed_executions: 128,
      profitable_executions: 84,
      losing_executions: 44,
    },
    trades: [
      {
        arb_id: "arb-older",
        status: "submitted",
        timestamp: 1_713_262_100,
        realized_pnl: 12.4,
        opportunity: { description: "Older trade" },
        leg_yes: { platform: "kalshi", price: 0.45, quantity: 100 },
        leg_no: { platform: "polymarket", price: 0.42, quantity: 100 },
      },
      {
        arb_id: "arb-newer",
        status: "filled",
        timestamp: 1_713_263_300,
        realized_pnl: 85.25,
        opportunity: { description: "Newest trade" },
        leg_yes: { platform: "kalshi", price: 0.47, quantity: 140 },
        leg_no: { platform: "predictit", price: 0.4, quantity: 140 },
      },
    ],
    incidents: [
      { incident_id: "inc-1", status: "open", severity: "critical", timestamp: 1_713_263_100 },
      { incident_id: "inc-2", status: "resolved", severity: "warning", timestamp: 1_713_262_900 },
    ],
    manualPositions: [{ position_id: "manual-1", status: "awaiting-entry", timestamp: 1_713_263_000 }],
    opportunities: [],
    lastQuoteAt: 1_713_263_580,
    wsConnected: true,
    ...overrides,
  };
}

describe("dashboard view model", () => {
  it("builds the hero, risk rail, and recent trades from live desk state", () => {
    const overview = buildDeskOverview(makeState(), { nowTimestamp: 1_713_263_600 });

    expect(overview.heroValue).toBe("$42,150.00");
    expect(overview.heroDelta).toBe("+$2,050.00");
    expect(overview.heroUpdated).toBe("Updated 20s ago");
    expect(overview.risk.percent).toBeGreaterThan(50);
    expect(overview.risk.summary).toContain("elevated");
    expect(overview.risk.items[0].label).toBe("Open incidents");
    expect(overview.recentTrades).toHaveLength(2);
    expect(overview.recentTrades[0].title).toBe("Newest trade");
    expect(overview.recentTrades[0].accent).toBe("positive");
  });

  it("builds the compact financial strip for the overview layout", () => {
    const cards = buildMetricCards(makeState());

    expect(cards).toHaveLength(4);
    expect(cards[0]).toMatchObject({
      label: "Realized P&L",
      value: "$1,859.48",
    });
    expect(cards[1].label).toBe("Open exposure");
    expect(cards[2].label).toBe("Validator progress");
    expect(cards[3].label).toBe("Trade throughput");
  });

  it("pins actionable blotter rows ahead of stale inventory", () => {
    const rows = buildOpportunityRows({
      opportunities: [
        { canonical_id: "stale-route", description: "Stale route", status: "stale", timestamp: 20, net_edge_cents: 4.4 },
        { canonical_id: "manual-route", description: "Manual route", status: "manual", timestamp: 10, net_edge_cents: 9.4 },
        { canonical_id: "tradable-route", description: "Tradable route", status: "tradable", timestamp: 30, net_edge_cents: 11.2 },
        { canonical_id: "review-route", description: "Review route", status: "review", timestamp: 40, net_edge_cents: 8.1 },
      ],
      system: {
        scanner: {
          persistence_scans: 3,
          max_quote_age_seconds: 15,
        },
      },
    });

    expect(rows.map((row) => row.canonicalId)).toEqual([
      "tradable-route",
      "manual-route",
      "review-route",
      "stale-route",
    ]);
  });
});
