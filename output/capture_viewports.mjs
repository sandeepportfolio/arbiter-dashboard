import { chromium, devices } from 'playwright';
const browser = await chromium.launch({ headless: true });
const scenarios = [
  { name: 'desktop-public-top', url: 'http://127.0.0.1:8090/', ctx: { viewport: { width: 1440, height: 1200 } } },
  { name: 'tablet-public-top', url: 'http://127.0.0.1:8090/', ctx: { viewport: { width: 1024, height: 1280 } } },
  { name: 'mobile-public-top', url: 'http://127.0.0.1:8090/', ctx: devices['iPhone 13'] },
  { name: 'desktop-ops-section', url: 'http://127.0.0.1:8090/?route=%2Fops', ctx: { viewport: { width: 1440, height: 1200 } }, ops: true },
];
for (const scenario of scenarios) {
  const context = await browser.newContext(scenario.ctx);
  const page = await context.newPage();
  await page.goto(scenario.url, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  if (scenario.ops) {
    await page.fill('#authEmail', 'operator@arbiter.local');
    await page.fill('#authPassword', 'secret');
    await page.click('#authSubmit');
    await page.waitForFunction(() => document.querySelector('#authOverlay')?.classList.contains('hidden'));
    await page.waitForTimeout(800);
    await page.locator('#opsSection').scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
  }
  await page.screenshot({ path: `output/playwright/${scenario.name}.png` });
  await context.close();
}
await browser.close();
