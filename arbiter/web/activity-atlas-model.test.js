import { describe, expect, it } from "vitest";
import { buildActivityAtlasRow, buildActivityAtlasView } from "./activity-atlas-model.js";

const scopeDefinitions = {
  all: {
    label: "All activity",
    categories: ["market", "opportunity", "execution", "manual", "incident", "balance", "collector", "mapping"],
  },
  trading: {
    label: "Trading flow",
    categories: ["market", "opportunity", "execution"],
  },
  ops: {
    label: "Ops workflow",
    categories: ["manual", "incident", "mapping"],
  },
};

const categoryDefinitions = {
  market: { label: "Market Pulse", digestLabel: "market pulses" },
  opportunity: { label: "Scanner", digestLabel: "scanner updates" },
  execution: { label: "Execution", digestLabel: "execution events" },
  manual: { label: "Manual Flow", digestLabel: "manual queue updates" },
  incident: { label: "Recovery", digestLabel: "recovery events" },
  balance: { label: "Balance", digestLabel: "balance checks" },
  collector: { label: "Collectors", digestLabel: "collector updates" },
  mapping: { label: "Mapping", digestLabel: "mapping updates" },
};

const filterOrder = ["all", "market", "opportunity", "execution", "manual", "incident", "balance", "collector", "mapping"];

const entries = [
  { category: "market", title: "Quote pulse", headline: "fresh", narrative: "Quotes are streaming", tags: ["market"], timestamp: 100 },
  { category: "execution", title: "Fill", headline: "settled", narrative: "Trade settled cleanly", tags: ["execution"], timestamp: 90 },
  { category: "manual", title: "Manual queue", headline: "awaiting", narrative: "Operator action required", tags: ["manual"], timestamp: 80 },
  { category: "mapping", title: "Mapping review", headline: "review", narrative: "Needs confirmation", tags: ["mapping"], timestamp: 70 },
];

describe("activity atlas model", () => {
  it("resets an invalid active filter when the selected scope no longer contains it", () => {
    const view = buildActivityAtlasView({
      entries,
      activeScope: "trading",
      activeFilter: "manual",
      query: "",
      scopeDefinitions,
      filterOrder,
    });

    expect(view.activeFilter).toBe("all");
    expect(view.filteredEntries).toHaveLength(2);
  });

  it("filters entries by search query across title, headline, narrative, and tags", () => {
    const view = buildActivityAtlasView({
      entries,
      activeScope: "all",
      activeFilter: "all",
      query: "operator",
      scopeDefinitions,
      filterOrder,
    });

    expect(view.filteredEntries).toHaveLength(1);
    expect(view.filteredEntries[0].category).toBe("manual");
  });

  it("builds filter chips only for categories visible in the active scope", () => {
    const view = buildActivityAtlasView({
      entries,
      activeScope: "ops",
      activeFilter: "all",
      query: "",
      scopeDefinitions,
      filterOrder,
    });

    expect(view.filterItems.map((item) => item.key)).toEqual(["all", "manual", "mapping"]);
    expect(view.categoryCounts.incident).toBe(0);
  });

  it("builds terse operational rows with title, meta, and compact tags", () => {
    const row = buildActivityAtlasRow(entries[0], {
      categoryDefinitions,
      nowTimestamp: 125,
    });

    expect(row.kind).toBe("entry");
    expect(row.titleLine).toBe("Quote pulse");
    expect(row.metaLine).toBe("fresh");
    expect(row.tags).toEqual(["Market Pulse", "25s ago", "market"]);
  });

  it("collapses bursty streams into digest rows in digest mode", () => {
    const burstEntries = [
      { category: "opportunity", title: "BTC route", headline: "tradable", narrative: "Route is live", tags: ["btc"], timestamp: 200, tone: "tone-mint", id: "opp-1" },
      { category: "opportunity", title: "ETH route", headline: "tradable", narrative: "Route is live", tags: ["eth"], timestamp: 188, tone: "tone-mint", id: "opp-2" },
      { category: "opportunity", title: "Fed route", headline: "review", narrative: "Needs confirmation", tags: ["fed"], timestamp: 180, tone: "tone-amber", id: "opp-3" },
      { category: "opportunity", title: "Oil route", headline: "stale", narrative: "Quote aged out", tags: ["oil"], timestamp: 170, tone: "tone-blue", id: "opp-4" },
      { category: "manual", title: "Manual queue", headline: "awaiting", narrative: "Operator action required", tags: ["manual"], timestamp: 120, tone: "tone-plum", id: "manual-1" },
    ];

    const view = buildActivityAtlasView({
      entries: burstEntries,
      activeScope: "all",
      activeFilter: "all",
      query: "",
      scopeDefinitions,
      filterOrder,
      categoryDefinitions,
      presentationMode: "digest",
      nowTimestamp: 200,
    });

    expect(view.filteredEntries).toHaveLength(5);
    expect(view.displayItems).toHaveLength(2);
    expect(view.displayItems[0]).toMatchObject({
      kind: "digest",
      category: "opportunity",
      count: 4,
      titleLine: "4 scanner updates",
    });
    expect(view.displayItems[0].tags).toContain("30s burst");
    expect(view.displayItems[1]).toMatchObject({
      kind: "entry",
      category: "manual",
      titleLine: "Manual queue",
    });
  });

  it("uses the highest-severity unresolved incident as the digest lead", () => {
    const burstEntries = [
      { category: "incident", title: "Recovered collector", headline: "Resolved warning collector incident", narrative: "Recovered cleanly", tags: ["resolved"], timestamp: 200, tone: "tone-blue", id: "inc-1", status: "resolved", severity: "warning" },
      { category: "incident", title: "One-leg mismatch", headline: "Open critical hedge incident", narrative: "Operator review required", tags: ["open"], timestamp: 190, tone: "tone-rose", id: "inc-2", status: "open", severity: "critical" },
      { category: "incident", title: "Cooldown expired", headline: "Resolved warning venue incident", narrative: "Venue recovered", tags: ["resolved"], timestamp: 175, tone: "tone-blue", id: "inc-3", status: "resolved", severity: "warning" },
    ];

    const view = buildActivityAtlasView({
      entries: burstEntries,
      activeScope: "all",
      activeFilter: "all",
      query: "",
      scopeDefinitions,
      filterOrder,
      categoryDefinitions,
      presentationMode: "digest",
      nowTimestamp: 200,
    });

    expect(view.displayItems).toHaveLength(1);
    expect(view.displayItems[0]).toMatchObject({
      kind: "digest",
      category: "incident",
      tone: "tone-rose",
      titleLine: "3 recovery events",
      metaLine: "Urgent One-leg mismatch - Open critical hedge incident",
      statusLabel: "Open Critical",
    });
    expect(view.displayItems[0].tags).toContain("Open Critical");
  });

  it("groups interleaved same-category bursts inside the digest window", () => {
    const burstEntries = [
      { category: "opportunity", title: "BTC route", headline: "tradable", narrative: "Route is live", tags: ["btc"], timestamp: 200, tone: "tone-mint", id: "opp-1" },
      { category: "manual", title: "Manual queue", headline: "awaiting", narrative: "Operator action required", tags: ["manual"], timestamp: 198, tone: "tone-plum", id: "manual-1" },
      { category: "opportunity", title: "ETH route", headline: "tradable", narrative: "Route is live", tags: ["eth"], timestamp: 188, tone: "tone-mint", id: "opp-2" },
      { category: "collector", title: "Collector cooldown", headline: "recovering", narrative: "Cooldown still active", tags: ["collector"], timestamp: 184, tone: "tone-blue", id: "collector-1" },
      { category: "opportunity", title: "Fed route", headline: "review", narrative: "Needs confirmation", tags: ["fed"], timestamp: 176, tone: "tone-amber", id: "opp-3" },
    ];

    const view = buildActivityAtlasView({
      entries: burstEntries,
      activeScope: "all",
      activeFilter: "all",
      query: "",
      scopeDefinitions,
      filterOrder,
      categoryDefinitions,
      presentationMode: "digest",
      nowTimestamp: 200,
    });

    expect(view.displayItems).toHaveLength(3);
    expect(view.displayItems[0]).toMatchObject({
      kind: "digest",
      category: "opportunity",
      count: 3,
      titleLine: "3 scanner updates",
    });
    expect(view.displayItems[1]).toMatchObject({
      kind: "entry",
      category: "manual",
    });
    expect(view.displayItems[2]).toMatchObject({
      kind: "entry",
      category: "collector",
    });
  });
});
