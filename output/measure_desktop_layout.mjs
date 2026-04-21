import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 2048, height: 1184 }, deviceScaleFactor: 1 });
await page.goto('http://127.0.0.1:8090', { waitUntil: 'networkidle' });
await page.waitForTimeout(800);
const data = await page.evaluate(() => {
  const pills = [...document.querySelectorAll('.chart-meta-pill')].map(el => {
    const r = el.getBoundingClientRect();
    return { w: r.width, h: r.height, text: el.textContent.trim() };
  });
  const cards = [...document.querySelectorAll('.blotter-row')].slice(0, 3).map(el => {
    const row = el.getBoundingClientRect();
    const title = el.querySelector('.blotter-row-title')?.getBoundingClientRect();
    const metrics = el.querySelector('.blotter-row-metrics')?.getBoundingClientRect();
    const side = el.querySelector('.blotter-row-side')?.getBoundingClientRect();
    return {
      row: { w: row.width, h: row.height },
      title: title && { x: title.x, y: title.y, w: title.width, h: title.height },
      metrics: metrics && { x: metrics.x, y: metrics.y, w: metrics.width, h: metrics.height },
      side: side && { x: side.x, y: side.y, w: side.width, h: side.height },
      text: el.textContent.replace(/\s+/g,' ').trim().slice(0,180),
    };
  });
  const perf = document.querySelector('#performanceSection')?.getBoundingClientRect();
  const chart = document.querySelector('.chart-panel')?.getBoundingClientRect();
  const right = document.querySelector('#opportunitiesSection')?.getBoundingClientRect();
  const overflow = document.documentElement.scrollWidth > window.innerWidth;
  return { pills, cards, perf, chart, right, overflow, bodyW: document.body.scrollWidth, innerW: window.innerWidth };
});
console.log(JSON.stringify(data, null, 2));
await browser.close();
