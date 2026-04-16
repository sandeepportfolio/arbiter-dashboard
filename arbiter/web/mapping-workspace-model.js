const formatWhole = new Intl.NumberFormat("en-US");

const PLATFORM_LABELS = {
  kalshi: "Kalshi",
  polymarket: "Polymarket",
  predictit: "PredictIt",
};

export const MAPPING_WORKSPACE_ROW_HEIGHT = 104;

function normalizeText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function titleCase(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function platformLabel(key) {
  return PLATFORM_LABELS[key] || titleCase(key);
}

function mappingStatus(mapping) {
  return String(mapping?.status || "candidate");
}

function mappingPriority(mapping) {
  const status = mapping.status;
  if (status === "review") return 0;
  if (status === "candidate") return 1;
  if (!mapping.allowAutoTrade) return 2;
  if (mapping.missingVenueCount > 0) return 3;
  return 4;
}

function buildMappingRecord(mapping, index) {
  const status = mappingStatus(mapping);
  const platforms = ["kalshi", "polymarket", "predictit"].map((key) => ({
    key,
    label: platformLabel(key),
    value: String(mapping?.[key] || "").trim(),
  }));
  const presentPlatforms = platforms.filter((platform) => platform.value);
  const missingPlatforms = platforms.filter((platform) => !platform.value);
  const title = String(mapping?.description || mapping?.canonical_id || `Mapping ${index + 1}`);
  const reviewNote = String(mapping?.review_note || "").trim();
  const notes = String(mapping?.notes || "").trim();
  const operatorNote = reviewNote || notes || "Confirmed mappings are the only auto-tradable candidates.";
  const allowAutoTrade = Boolean(mapping?.allow_auto_trade);

  return {
    canonicalId: String(mapping?.canonical_id || `mapping-${index + 1}`),
    title,
    status,
    statusLabel: titleCase(status),
    allowAutoTrade,
    allowAutoTradeLabel: allowAutoTrade ? "Auto-trade allowed" : "Held for operator review",
    reviewNote,
    notes,
    operatorNote,
    description: title,
    platforms: presentPlatforms.map((platform) => ({
      ...platform,
      chipLabel: `${platform.label} ${platform.value}`,
    })),
    missingPlatforms: missingPlatforms.map((platform) => platform.label),
    venueCount: presentPlatforms.length,
    missingVenueCount: missingPlatforms.length,
    coverageLabel: presentPlatforms.length === 3
      ? "Full venue coverage"
      : `${formatWhole.format(missingPlatforms.length)} venue ${missingPlatforms.length === 1 ? "gap" : "gaps"}`,
    isActionable: status !== "confirmed" || !allowAutoTrade,
    searchText: normalizeText([
      mapping?.canonical_id,
      title,
      status,
      reviewNote,
      notes,
      ...platforms.flatMap((platform) => [platform.label, platform.value]),
    ].join(" ")),
  };
}

const VIEW_DEFINITIONS = [
  {
    key: "review",
    label: "Review queue",
    description: "Mappings still waiting on operator confirmation or auto-trade approval.",
    predicate: (mapping) => mapping.isActionable,
  },
  {
    key: "coverage",
    label: "Coverage gaps",
    description: "Venue ids are missing and the route still needs reconciliation.",
    predicate: (mapping) => mapping.missingVenueCount > 0,
  },
  {
    key: "auto",
    label: "Auto-trade ready",
    description: "Confirmed mappings that can re-enter the scanner automatically.",
    predicate: (mapping) => mapping.allowAutoTrade,
  },
  {
    key: "notes",
    label: "Notes",
    description: "Mappings carrying operator notes or review copy.",
    predicate: (mapping) => Boolean(mapping.reviewNote || mapping.notes),
  },
  {
    key: "all",
    label: "All mappings",
    description: "The complete canonical market map.",
    predicate: () => true,
  },
];

function compareMappings(left, right) {
  const priorityDelta = mappingPriority(left) - mappingPriority(right);
  if (priorityDelta !== 0) return priorityDelta;
  if (left.missingVenueCount !== right.missingVenueCount) return right.missingVenueCount - left.missingVenueCount;
  return left.title.localeCompare(right.title, "en", { sensitivity: "base", numeric: true });
}

function resolveActiveView(requestedKey, views) {
  if (requestedKey && views.some((view) => view.key === requestedKey)) {
    return requestedKey;
  }
  const actionableView = views.find((view) => view.key === "review" && view.count > 0);
  return actionableView ? actionableView.key : "all";
}

export function buildMappingWorkspaceModel({
  mappings = [],
  activeView = "",
  query = "",
  selectedId = "",
  scrollTop = 0,
  viewportHeight = 0,
  rowHeight = MAPPING_WORKSPACE_ROW_HEIGHT,
  overscan = 2,
} = {}) {
  const records = mappings.map(buildMappingRecord).sort(compareMappings);
  const views = VIEW_DEFINITIONS.map((view) => ({
    key: view.key,
    label: view.label,
    description: view.description,
    count: records.filter(view.predicate).length,
  }));
  const activeViewKey = resolveActiveView(activeView, views);
  const activeViewDef = VIEW_DEFINITIONS.find((view) => view.key === activeViewKey) || VIEW_DEFINITIONS[VIEW_DEFINITIONS.length - 1];
  const queryValue = String(query || "");
  const normalizedQuery = normalizeText(queryValue);
  const queryTerms = normalizedQuery ? normalizedQuery.split(" ").filter(Boolean) : [];

  const filteredMappings = records.filter((mapping) => {
    if (!activeViewDef.predicate(mapping)) return false;
    if (!queryTerms.length) return true;
    return queryTerms.every((term) => mapping.searchText.includes(term));
  });

  const selectedMapping = filteredMappings.find((mapping) => mapping.canonicalId === selectedId) || filteredMappings[0] || null;
  const resolvedSelectedId = selectedMapping?.canonicalId || "";
  const safeRowHeight = Math.max(48, Number(rowHeight || MAPPING_WORKSPACE_ROW_HEIGHT));
  const safeViewportHeight = Math.max(safeRowHeight, Number(viewportHeight || safeRowHeight * 4));
  const startIndex = Math.max(0, Math.floor(Math.max(0, Number(scrollTop || 0)) / safeRowHeight) - overscan);
  const visibleCount = Math.max(1, Math.ceil(safeViewportHeight / safeRowHeight) + (overscan * 2));
  const endIndex = Math.min(filteredMappings.length, startIndex + visibleCount);
  const listRows = filteredMappings.slice(startIndex, endIndex).map((mapping, index) => ({
    ...mapping,
    rowIndex: startIndex + index,
    isSelected: mapping.canonicalId === resolvedSelectedId,
  }));

  return {
    views,
    activeView: views.find((view) => view.key === activeViewKey) || views[views.length - 1],
    totalCount: records.length,
    filteredCount: filteredMappings.length,
    query: queryValue,
    selectedId: resolvedSelectedId,
    selectedMapping,
    listRows,
    topSpacerHeight: startIndex * safeRowHeight,
    bottomSpacerHeight: Math.max(0, (filteredMappings.length - endIndex) * safeRowHeight),
    rowHeight: safeRowHeight,
    emptyMessage: records.length === 0
      ? "No canonical mappings are loaded."
      : queryTerms.length
        ? "No mappings match this queue search."
        : "No mappings match this saved view right now.",
  };
}
