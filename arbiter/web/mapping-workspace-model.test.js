import { describe, expect, it } from "vitest";
import { buildMappingWorkspaceModel } from "./mapping-workspace-model.js";

function makeMapping(overrides = {}) {
  return {
    canonical_id: "mapping-1",
    description: "Mapping 1",
    status: "confirmed",
    allow_auto_trade: true,
    notes: "",
    review_note: "",
    kalshi: "KALSHI-1",
    polymarket: "POLY-1",
    predictit: "PREDICTIT-1",
    ...overrides,
  };
}

describe("mapping workspace model", () => {
  it("builds saved views and defaults to the review queue when action is needed", () => {
    const model = buildMappingWorkspaceModel({
      mappings: [
        makeMapping(),
        makeMapping({
          canonical_id: "fed-cut-july",
          description: "Fed rate cut by July",
          status: "review",
          allow_auto_trade: false,
          review_note: "PredictIt wording still needs manual confirmation.",
          polymarket: "",
        }),
        makeMapping({
          canonical_id: "house-majority",
          description: "House majority control",
          status: "candidate",
          allow_auto_trade: false,
          notes: "Coverage gap while venue ids are reconciled.",
          polymarket: "",
          predictit: "",
        }),
        makeMapping({
          canonical_id: "oil-above-95",
          description: "Oil above $95",
          status: "confirmed",
          allow_auto_trade: false,
          notes: "Held until operator review signs off.",
        }),
      ],
      viewportHeight: 216,
      scrollTop: 0,
    });

    expect(model.activeView.key).toBe("review");
    expect(model.views.find((view) => view.key === "review")).toMatchObject({ count: 3 });
    expect(model.views.find((view) => view.key === "coverage")).toMatchObject({ count: 2 });
    expect(model.views.find((view) => view.key === "auto")).toMatchObject({ count: 1 });
    expect(model.filteredCount).toBe(3);
    expect(model.selectedMapping.canonicalId).toBe("fed-cut-july");
  });

  it("filters mappings by queue view and search terms across ids, venues, and notes", () => {
    const model = buildMappingWorkspaceModel({
      mappings: [
        makeMapping(),
        makeMapping({
          canonical_id: "fed-cut-july",
          description: "Fed rate cut by July",
          status: "review",
          allow_auto_trade: false,
          review_note: "PredictIt wording still needs manual confirmation.",
          predictit: "PI-FEDCUT-JULY",
        }),
      ],
      activeView: "all",
      query: "pi-fedcut wording",
      viewportHeight: 216,
      scrollTop: 0,
    });

    expect(model.filteredCount).toBe(1);
    expect(model.selectedMapping.canonicalId).toBe("fed-cut-july");
    expect(model.listRows).toHaveLength(1);
  });

  it("falls back to the first visible mapping when the prior selection is filtered out", () => {
    const model = buildMappingWorkspaceModel({
      mappings: [
        makeMapping(),
        makeMapping({
          canonical_id: "fed-cut-july",
          description: "Fed rate cut by July",
          status: "review",
          allow_auto_trade: false,
        }),
      ],
      activeView: "review",
      selectedId: "mapping-1",
      viewportHeight: 216,
      scrollTop: 0,
    });

    expect(model.selectedId).toBe("fed-cut-july");
    expect(model.selectedMapping.canonicalId).toBe("fed-cut-july");
  });

  it("virtualizes long mapping lists into a bounded visible window", () => {
    const mappings = Array.from({ length: 20 }, (_, index) => makeMapping({
      canonical_id: `mapping-${index + 1}`,
      description: `Mapping ${index + 1}`,
      status: "confirmed",
      allow_auto_trade: true,
    }));
    const model = buildMappingWorkspaceModel({
      mappings,
      activeView: "all",
      viewportHeight: 144,
      rowHeight: 72,
      scrollTop: 288,
    });

    expect(model.filteredCount).toBe(20);
    expect(model.listRows.length).toBeLessThan(model.filteredCount);
    expect(model.topSpacerHeight).toBe(144);
    expect(model.listRows[0].canonicalId).toBe("mapping-3");
    expect(model.bottomSpacerHeight).toBeGreaterThan(0);
  });
});
