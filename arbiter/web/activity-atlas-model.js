const DIGEST_MIN_ITEMS = 3;
const DIGEST_WINDOW_SECONDS = 90;
const SEVERITY_RANK = {
  critical: 4,
  high: 3,
  error: 3,
  warning: 2,
  review: 1,
  info: 0,
  low: 0,
};
const TONE_RANK = {
  "tone-rose": 4,
  "tone-amber": 3,
  "tone-plum": 2,
  "tone-gold": 2,
  "tone-blue": 1,
  "tone-mint": 1,
  "tone-slate": 0,
};

function titleCase(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function emptyCategoryCounts(filterOrder) {
  return filterOrder
    .filter((key) => key !== "all")
    .reduce((counts, key) => {
      counts[key] = 0;
      return counts;
    }, {});
}

function buildCategoryCounts(entries, filterOrder) {
  return entries.reduce((counts, entry) => {
    if (entry?.category in counts) {
      counts[entry.category] += 1;
    }
    return counts;
  }, emptyCategoryCounts(filterOrder));
}

function buildScopeCounts(entries, scopeDefinitions) {
  return Object.entries(scopeDefinitions).reduce((counts, [key, definition]) => {
    if (key === "all") {
      counts[key] = entries.length;
      return counts;
    }
    const allowed = new Set(definition.categories);
    counts[key] = entries.filter((entry) => allowed.has(entry.category)).length;
    return counts;
  }, {});
}

function matchesQuery(entry, query) {
  if (!query) return true;
  const haystack = [
    entry.title,
    entry.headline,
    entry.narrative,
    ...(entry.tags || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  return haystack.includes(query);
}

function formatRelativeAge(timestamp, nowTimestamp) {
  if (!timestamp || !nowTimestamp) return "now";
  const delta = Math.max(0, Math.round(nowTimestamp - timestamp));
  if (delta < 10) return "now";
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  return `${Math.round(delta / 3600)}h ago`;
}

function formatBurstSpan(seconds) {
  if (seconds < 60) return `${seconds}s burst`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m burst`;
  return `${Math.round(seconds / 3600)}h burst`;
}

function compactTags(parts, limit = 4) {
  const seen = new Set();
  const tags = [];

  for (const part of parts) {
    const normalized = String(part || "").trim();
    if (!normalized) continue;
    const dedupeKey = normalized.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    tags.push(normalized);
    if (tags.length >= limit) break;
  }

  return tags;
}

function resolveCategoryLabel(category, categoryDefinitions) {
  return categoryDefinitions?.[category]?.label || titleCase(category);
}

function resolveDigestLabel(category, categoryDefinitions) {
  return categoryDefinitions?.[category]?.digestLabel || `${resolveCategoryLabel(category, categoryDefinitions).toLowerCase()} updates`;
}

function severityRank(value) {
  return SEVERITY_RANK[String(value || "").toLowerCase()] ?? -1;
}

function toneRank(value) {
  return TONE_RANK[String(value || "")] ?? -1;
}

function isResolvedStatus(value) {
  return ["resolved", "closed", "filled", "confirmed", "manual_closed"].includes(String(value || "").toLowerCase());
}

function buildStatusLabel(status, severity) {
  if (status && severity) return `${titleCase(status)} ${titleCase(severity)}`;
  if (status) return titleCase(status);
  if (severity) return titleCase(severity);
  return "";
}

function compareDigestPriority(left, right) {
  const unresolvedDelta = Number(!isResolvedStatus(right.status)) - Number(!isResolvedStatus(left.status));
  if (unresolvedDelta !== 0) return unresolvedDelta;

  const severityDelta = severityRank(right.severity) - severityRank(left.severity);
  if (severityDelta !== 0) return severityDelta;

  const toneDelta = toneRank(right.tone) - toneRank(left.tone);
  if (toneDelta !== 0) return toneDelta;

  return (right.timestamp || 0) - (left.timestamp || 0);
}

function selectDigestLead(group) {
  return [...group].sort(compareDigestPriority)[0];
}

export function buildActivityAtlasRow(entry, { categoryDefinitions = {}, nowTimestamp = 0 } = {}) {
  const categoryLabel = resolveCategoryLabel(entry.category, categoryDefinitions);
  const metaLine = String(entry.headline || entry.narrative || `${categoryLabel} update`).trim();
  const statusLabel = String(entry.statusLabel || buildStatusLabel(entry.status, entry.severity)).trim();

  return {
    kind: "entry",
    id: entry.id,
    category: entry.category,
    tone: entry.tone,
    timestamp: entry.timestamp || 0,
    titleLine: String(entry.title || categoryLabel).trim(),
    metaLine,
    tags: compactTags([
      categoryLabel,
      statusLabel,
      formatRelativeAge(entry.timestamp || 0, nowTimestamp),
      ...(entry.tags || []),
    ]),
    sourceLabel: entry.footnote || "",
    status: entry.status || "",
    severity: entry.severity || "",
    statusLabel,
  };
}

function buildDigestItem(group, { categoryDefinitions = {} } = {}) {
  const latestItem = [...group].sort((left, right) => (right.timestamp || 0) - (left.timestamp || 0))[0];
  const lead = selectDigestLead(group);
  const latestTimestamp = latestItem?.timestamp || 0;
  const earliestTimestamp = group.reduce((minTimestamp, item) => Math.min(minTimestamp, item.timestamp || latestTimestamp), latestTimestamp);
  const uniqueTitles = new Set(group.map((item) => item.titleLine).filter(Boolean)).size;
  const leadPrefix = lead.id !== latestItem?.id && !isResolvedStatus(lead.status) ? "Urgent" : "Latest";

  return {
    kind: "digest",
    id: `${lead.category}-digest-${lead.id || latestTimestamp}`,
    category: lead.category,
    tone: lead.tone,
    timestamp: latestTimestamp,
    count: group.length,
    titleLine: `${group.length} ${resolveDigestLabel(lead.category, categoryDefinitions)}`,
    metaLine: `${leadPrefix} ${lead.titleLine} - ${lead.metaLine}`,
    tags: compactTags([
      formatBurstSpan(Math.max(0, Math.round(latestTimestamp - earliestTimestamp))),
      `${group.length} events`,
      lead.statusLabel,
      uniqueTitles > 1 ? `${uniqueTitles} routes` : lead.titleLine,
      resolveCategoryLabel(lead.category, categoryDefinitions),
    ]),
    sourceLabel: lead.sourceLabel,
    status: lead.status,
    severity: lead.severity,
    statusLabel: lead.statusLabel || "",
  };
}

function buildDisplayItems(entries, { presentationMode = "stream", categoryDefinitions = {}, nowTimestamp = 0 } = {}) {
  const compactRows = entries.map((entry) => buildActivityAtlasRow(entry, { categoryDefinitions, nowTimestamp }));
  if (presentationMode !== "digest") return compactRows;

  const items = [];
  const consumed = new Set();

  for (let index = 0; index < compactRows.length; index += 1) {
    if (consumed.has(index)) continue;
    const seed = compactRows[index];
    const group = [seed];

    for (let cursor = index + 1; cursor < compactRows.length; cursor += 1) {
      if (consumed.has(cursor)) continue;
      const candidate = compactRows[cursor];
      const withinBurstWindow = (seed.timestamp || 0) - (candidate.timestamp || 0) <= DIGEST_WINDOW_SECONDS;
      if (candidate.category !== seed.category || !withinBurstWindow) continue;
      group.push(candidate);
    }

    if (group.length >= DIGEST_MIN_ITEMS) {
      items.push(buildDigestItem(group, { categoryDefinitions }));
      group.forEach((item) => {
        const position = compactRows.indexOf(item);
        if (position >= 0) consumed.add(position);
      });
      continue;
    }

    consumed.add(index);
    items.push(seed);
  }

  return items;
}

export function buildActivityAtlasView({
  entries,
  activeScope,
  activeFilter,
  query,
  scopeDefinitions,
  filterOrder,
  categoryDefinitions = {},
  presentationMode = "stream",
  nowTimestamp = 0,
}) {
  const scope = scopeDefinitions[activeScope] || scopeDefinitions.all;
  const scopeCounts = buildScopeCounts(entries, scopeDefinitions);
  const scopedEntries = activeScope === "all"
    ? entries
    : entries.filter((entry) => scope.categories.includes(entry.category));

  const categoryCounts = buildCategoryCounts(scopedEntries, filterOrder);
  const normalizedFilter = activeFilter !== "all" && !(categoryCounts[activeFilter] > 0)
    ? "all"
    : activeFilter;
  const scopeFilteredEntries = normalizedFilter === "all"
    ? scopedEntries
    : scopedEntries.filter((entry) => entry.category === normalizedFilter);
  const normalizedQuery = String(query || "").trim().toLowerCase();
  const filteredEntries = scopeFilteredEntries.filter((entry) => matchesQuery(entry, normalizedQuery));
  const filterItems = filterOrder
    .filter((key) => key === "all" || (scope.categories.includes(key) && (categoryCounts[key] || 0) > 0))
    .map((key) => ({
      key,
      count: key === "all" ? scopedEntries.length : (categoryCounts[key] || 0),
    }));
  const effectiveNow = nowTimestamp || filteredEntries[0]?.timestamp || entries[0]?.timestamp || 0;

  return {
    scope,
    scopeCounts,
    scopedEntries,
    categoryCounts,
    activeFilter: normalizedFilter,
    filteredEntries,
    filterItems,
    query: normalizedQuery,
    presentationMode,
    displayItems: buildDisplayItems(filteredEntries, {
      presentationMode,
      categoryDefinitions,
      nowTimestamp: effectiveNow,
    }),
  };
}
