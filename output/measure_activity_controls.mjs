import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
await page.goto('http://127.0.0.1:8090/', { waitUntil: 'networkidle' });
await page.locator('#logsSection').scrollIntoViewIfNeeded();
await page.waitForTimeout(500);

async function rects(selector) {
  return await page.locator(selector).evaluateAll((els) => els.map((el) => {
    const r = el.getBoundingClientRect();
    return {
      text: el.textContent.replace(/\s+/g, ' ').trim(),
      width: Math.round(r.width),
      height: Math.round(r.height),
      x: Math.round(r.x),
      y: Math.round(r.y),
      cls: el.className,
    };
  }));
}

const before = {
  scope: await rects('.log-scope-chip'),
  filter: await rects('.log-filter-chip'),
  category: await rects('.log-category-card'),
};

await page.locator('.log-scope-chip').nth(1).click();
await page.waitForTimeout(250);
await page.locator('.log-filter-chip').nth(1).click();
await page.waitForTimeout(250);
await page.locator('.log-category-card').nth(0).click();
await page.waitForTimeout(250);

const after = {
  scope: await rects('.log-scope-chip'),
  filter: await rects('.log-filter-chip'),
  category: await rects('.log-category-card'),
};

console.log(JSON.stringify({ before, after }, null, 2));
await browser.close();
