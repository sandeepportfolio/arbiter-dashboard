import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const opsHtml = readFileSync(new URL("./ops.html", import.meta.url), "utf8");
const appShellSource = readFileSync(new URL("./redesign/app-shell.jsx", import.meta.url), "utf8");
const mobileSource = readFileSync(new URL("./redesign/mobile.jsx", import.meta.url), "utf8");

function functionBody(name) {
  const start = opsHtml.indexOf(`function ${name}(`);
  expect(start, `${name} should exist`).toBeGreaterThanOrEqual(0);
  const nextFunction = opsHtml.indexOf("\nfunction ", start + 1);
  return opsHtml.slice(start, nextFunction > start ? nextFunction : undefined);
}

function sliceBetween(source, startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  expect(start, `${startNeedle} should exist`).toBeGreaterThanOrEqual(0);
  const end = source.indexOf(endNeedle, start);
  expect(end, `${endNeedle} should exist`).toBeGreaterThan(start);
  return source.slice(start, end);
}

function makeBrowserContext({ hash = "", stored = {} } = {}) {
  const storage = new Map(Object.entries(stored));
  const location = { hash, pathname: "/ops", search: "" };
  return {
    window: {
      location,
      history: {
        replaceState: (_state, _title, url) => {
          if (String(url).startsWith("#")) {
            location.hash = String(url);
          } else {
            location.hash = "";
          }
        },
      },
      addEventListener: () => {},
      removeEventListener: () => {},
    },
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
  };
}

function evalPageHelpers(source, context) {
  const snippet = sliceBetween(source, "const ARB_PAGE_KEY", "function useTheme");
  vm.runInNewContext(
    `${snippet}\nglobalThis.__helpers = { normalizePageId, readStoredPage, persistPage, ARB_PAGE_KEY };`,
    context,
  );
  return context.__helpers;
}

function evalMobileHelpers(source, context) {
  const snippet = sliceBetween(source, "const ARB_MOBILE_TAB_KEY", "function MobileDashboard");
  vm.runInNewContext(
    `${snippet}\nglobalThis.__helpers = { readStoredMobileTab, persistMobileTab, ARB_MOBILE_TAB_KEY };`,
    context,
  );
  return context.__helpers;
}

describe("ops mobile row interactions", () => {
  it("opens the trade detail modal when a mobile trade card is tapped", () => {
    const body = functionBody("MobTrades");

    expect(body).toContain("setModal({ kind:'trade'");
    expect(body).toContain("onClick={() => setModal({ kind:'trade'");
  });

  it("opens validation history for every mapping row on desktop and mobile", () => {
    const desktop = functionBody("PageMappings");
    const mobile = functionBody("MobMappings");

    expect(desktop).toContain("onRowClick={(r) => setModal({ kind:'agentValidate', payload: r })}");
    expect(mobile).toContain("const openCard = () => setModal({ kind:'agentValidate', payload: c })");
    expect(mobile).not.toContain(": setModal({ kind:'market'");
  });
});

describe("ops mapping desk controls", () => {
  it("renders clickable mapping filters and wires summary cards to them", () => {
    const desktop = functionBody("PageMappings");
    const mobile = functionBody("MobMappings");

    expect(desktop).toContain("mappingFilter");
    expect(desktop).toContain("MAPPING_FILTERS");
    expect(desktop).toContain("setMappingFilter('confirmed')");
    expect(desktop).toContain("setMappingFilter('pending')");
    expect(desktop).toContain("setModal({ kind:'refetchMappings'");
    expect(desktop).toContain("rows={filteredCandidates}");
    expect(mobile).toContain("MOBILE_MAPPING_FILTERS");
    expect(mobile).toContain("setMappingFilter");
  });

  it("shows live Claude CLI validation progress before the backend run finishes", () => {
    const session = functionBody("AgentSession");

    expect(session).toContain("run.starting claude_code_cli=true");
    expect(session).toContain("validationProgressTimersRef");
    expect(session).toContain("clearValidationProgressTimers");
    expect(session).toContain("Claude Code CLI is running");
  });

  it("lets the last-refetch card inspect history without auto-starting a run", () => {
    const mappings = functionBody("PageMappings");
    const refetch = functionBody("RefetchSession");

    expect(mappings).toContain("inspectOnly:true");
    expect(refetch).toContain("job.inspectOnly !== true");
    expect(refetch).toContain("startDiscoveryRun");
    expect(refetch).toContain("Recent run history");
    expect(refetch).toContain("Run now");
  });
});

describe("ops desktop balance cards", () => {
  it("renders the total balance platform split as structured rows", () => {
    const overview = functionBody("PageOverview");
    const kpiCard = functionBody("KpiCard");

    expect(overview).toContain("platformBalances={[");
    expect(overview).toContain("name: 'Polymarket'");
    expect(overview).not.toContain("Poly ${window.fmt$");
    expect(kpiCard).toContain("platformBalances");
    expect(kpiCard).toContain("platformBalances.map");
    expect(kpiCard).toContain("lineHeight: 1.35");
  });
});

describe("ops refresh persistence", () => {
  it.each([
    ["bundled ops.html", opsHtml],
    ["source app-shell.jsx", appShellSource],
  ])("restores and persists the selected desktop page from %s", (_label, source) => {
    const storedContext = makeBrowserContext({ stored: { "arbiter-current-page": "mappings" } });
    const storedHelpers = evalPageHelpers(source, storedContext);
    expect(storedHelpers.readStoredPage()).toBe("mappings");

    const hashContext = makeBrowserContext({ hash: "#trades", stored: { "arbiter-current-page": "mappings" } });
    const hashHelpers = evalPageHelpers(source, hashContext);
    expect(hashHelpers.readStoredPage()).toBe("trades");

    const staleHashContext = makeBrowserContext({ hash: "#unknown", stored: { "arbiter-current-page": "mappings" } });
    const staleHashHelpers = evalPageHelpers(source, staleHashContext);
    expect(staleHashHelpers.readStoredPage()).toBe("mappings");

    staleHashHelpers.persistPage("scanner");
    expect(staleHashContext.localStorage.getItem("arbiter-current-page")).toBe("scanner");
    expect(staleHashContext.window.location.hash).toBe("#scanner");
  });

  it.each([
    ["bundled ops.html", opsHtml],
    ["source mobile.jsx", mobileSource],
  ])("restores and persists the selected mobile tab from %s", (_label, source) => {
    const context = makeBrowserContext({ stored: { "arbiter-mobile-tab": "maps" } });
    const helpers = evalMobileHelpers(source, context);

    expect(helpers.readStoredMobileTab()).toBe("maps");
    helpers.persistMobileTab("trades");
    expect(context.localStorage.getItem("arbiter-mobile-tab")).toBe("trades");
    helpers.persistMobileTab("bogus");
    expect(context.localStorage.getItem("arbiter-mobile-tab")).toBe("trades");
  });

  it("does not clear a signed-in session for transient auth checks", () => {
    expect(opsHtml).toContain("const explicitAuthFailure = res.status === 401 || res.status === 403");
    expect(opsHtml).toContain("return { ok: false, status: r.status, error: await readError(r) }");
    expect(opsHtml).toContain("status: 0");
  });
});
