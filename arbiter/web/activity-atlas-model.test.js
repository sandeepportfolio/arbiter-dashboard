import { describe, expect, it } from "vitest";
import { buildActivityAtlasView } from "./activity-atlas-model.js";

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
});
