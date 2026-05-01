import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const opsHtml = readFileSync(new URL("./ops.html", import.meta.url), "utf8");

function functionBody(name) {
  const start = opsHtml.indexOf(`function ${name}(`);
  expect(start, `${name} should exist`).toBeGreaterThanOrEqual(0);
  const nextFunction = opsHtml.indexOf("\nfunction ", start + 1);
  return opsHtml.slice(start, nextFunction > start ? nextFunction : undefined);
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
  it("restores the selected desktop page after a browser refresh", () => {
    const provider = functionBody("AppProvider");

    expect(opsHtml).toContain("const ARB_PAGE_KEY");
    expect(provider).toContain("readStoredPage");
    expect(provider).toContain("persistPage(pageId)");
    expect(provider).toContain("hashchange");
  });

  it("restores the selected mobile tab after a browser refresh", () => {
    const mobile = functionBody("MobileDashboard");

    expect(opsHtml).toContain("const ARB_MOBILE_TAB_KEY");
    expect(mobile).toContain("readStoredMobileTab");
    expect(mobile).toContain("persistMobileTab(k)");
  });

  it("does not clear a signed-in session for transient auth checks", () => {
    expect(opsHtml).toContain("const explicitAuthFailure = res.status === 401 || res.status === 403");
    expect(opsHtml).toContain("return { ok: false, status: r.status, error: await readError(r) }");
    expect(opsHtml).toContain("status: 0");
  });
});
