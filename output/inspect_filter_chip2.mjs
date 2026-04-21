import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
await page.goto('http://127.0.0.1:8090/', { waitUntil: 'networkidle' });
await page.locator('#logsSection').scrollIntoViewIfNeeded();
await page.waitForTimeout(500);
await page.locator('.log-scope-chip').nth(1).click();
await page.waitForTimeout(250);
await page.locator('.log-filter-chip').nth(1).click();
await page.waitForTimeout(250);
await page.locator('.log-category-card').nth(0).click();
await page.waitForTimeout(250);
const data = await page.locator('.log-filter-chip').nth(0).evaluate((el) => {
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const parent = getComputedStyle(el.parentElement);
  return {
    text: el.textContent.replace(/\s+/g, ' ').trim(),
    rect: { width: r.width, height: r.height, x: r.x, y: r.y },
    display: cs.display,
    alignItems: cs.alignItems,
    justifyContent: cs.justifyContent,
    width: cs.width,
    height: cs.height,
    minHeight: cs.minHeight,
    padding: cs.padding,
    parentAlignItems: parent.alignItems,
    parentDisplay: parent.display,
    parentFlexWrap: parent.flexWrap,
    parentHeight: parent.height,
    html: el.parentElement.innerHTML,
  };
});
console.log(JSON.stringify(data, null, 2));
await browser.close();
