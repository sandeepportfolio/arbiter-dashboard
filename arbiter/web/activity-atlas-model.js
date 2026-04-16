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

export function buildActivityAtlasView({
  entries,
  activeScope,
  activeFilter,
  query,
  scopeDefinitions,
  filterOrder,
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

  return {
    scope,
    scopeCounts,
    scopedEntries,
    categoryCounts,
    activeFilter: normalizedFilter,
    filteredEntries,
    filterItems,
    query: normalizedQuery,
  };
}
