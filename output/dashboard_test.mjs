import { chromium } from 'playwright';

const BASE = 'http://127.0.0.1:8080';
const OUT = 'C:/Users/sande/Documents/arbiter-dashboard/output/dashboard-test';

const browser = await chromium.launchPersistentContext(
  'C:/Users/sande/Documents/arbiter-dashboard/.playwright-dashboard-test/',
  { headless: true, viewport: { width: 1440, height: 900 } }
);
const page = await browser.pages()[0] ?? await browser.newPage();

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
page.on('console', m => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
page.on('pageerror', e => pageErrors.push(String(e)));
page.on('requestfailed', r => failedRequests.push(`${r.url()}  ${r.failure()?.errorText ?? 'unknown'}`));

async function test(path, name, viewport) {
  if (viewport) await page.setViewportSize(viewport);
  const start = Date.now();
  const res = await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle', timeout: 20000 });
  const elapsed = Date.now() - start;
  await page.waitForTimeout(1500);
  const pngPath = `${OUT}/${name}.png`;
  await page.screenshot({ path: pngPath, fullPage: true });
  return { path, name, status: res.status(), elapsed, pngPath };
}

const desktop = { width: 1440, height: 900 };
const mobile = { width: 390, height: 844 };

const results = [];
results.push(await test('/', 'public-desktop', desktop));
results.push(await test('/ops', 'ops-desktop', desktop));
results.push(await test('/ops', 'ops-mobile', mobile));

// DOM sanity checks on /ops
await page.setViewportSize(desktop);
await page.goto(`${BASE}/ops`, { waitUntil: 'networkidle' });
await page.waitForTimeout(2000);

const checks = await page.evaluate(() => {
  const q = (sel) => !!document.querySelector(sel);
  const txt = (sel) => document.querySelector(sel)?.textContent?.trim()?.slice(0, 80) ?? null;
  return {
    has_title: !!document.title,
    title: document.title,
    has_heroValue: q('#heroValue'),
    has_connectionOverlay: q('#connectionOverlay'),
    has_authOverlay: q('#authOverlay'),
    has_equityChart: q('#equityChart'),
    body_text_len: document.body.innerText.length,
    body_text_snippet: document.body.innerText.slice(0, 300),
    ws_scripts: document.querySelectorAll('script[type=module]').length,
    has_kalshi_text: /kalshi/i.test(document.body.innerText),
    has_polymarket_text: /polymarket/i.test(document.body.innerText),
    has_predictit_text: /predictit/i.test(document.body.innerText),
  };
});

await browser.close();

console.log('=== Pages ===');
for (const r of results) {
  console.log(`  ${r.name.padEnd(16)}  ${r.path.padEnd(5)}  HTTP ${r.status}  ${String(r.elapsed).padStart(5)}ms  -> ${r.pngPath}`);
}
console.log('\n=== DOM checks on /ops ===');
for (const [k, v] of Object.entries(checks)) console.log(`  ${k.padEnd(26)}  ${String(v).slice(0, 120)}`);
console.log('\n=== Console errors ===');
if (consoleErrors.length === 0) console.log('  (none)');
for (const e of consoleErrors.slice(0, 10)) console.log(`  - ${e.slice(0, 200)}`);
console.log('\n=== Page errors ===');
if (pageErrors.length === 0) console.log('  (none)');
for (const e of pageErrors.slice(0, 10)) console.log(`  - ${e.slice(0, 200)}`);
console.log('\n=== Failed requests ===');
if (failedRequests.length === 0) console.log('  (none)');
for (const r of failedRequests.slice(0, 10)) console.log(`  - ${r.slice(0, 200)}`);
