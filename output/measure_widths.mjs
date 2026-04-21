import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
for (const width of [1600, 1440, 1366, 1280, 1120]) {
  const page = await browser.newPage({ viewport: { width, height: 1184 }, deviceScaleFactor: 1 });
  await page.goto('http://127.0.0.1:8090', { waitUntil: 'networkidle' });
  await page.waitForTimeout(400);
  const data = await page.evaluate(() => {
    const topPills = [...document.querySelectorAll('.chart-meta-pill')].slice(0, 5).map(el => {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return { w: Number(r.width.toFixed(1)), h: Number(r.height.toFixed(1)), display: style.display, text: el.textContent.replace(/\s+/g,' ').trim() };
    });
    const opp = document.querySelector('#opportunityList .blotter-row');
    const row = opp?.getBoundingClientRect();
    const title = opp?.querySelector('.blotter-row-title')?.getBoundingClientRect();
    const metrics = opp?.querySelector('.blotter-row-metrics')?.getBoundingClientRect();
    const side = opp?.querySelector('.blotter-row-side')?.getBoundingClientRect();
    const perf = document.querySelector('#performanceSection')?.getBoundingClientRect();
    return {
      topPills,
      row: row && { w: Number(row.width.toFixed(1)), h: Number(row.height.toFixed(1)) },
      title: title && { w: Number(title.width.toFixed(1)), h: Number(title.height.toFixed(1)) },
      metrics: metrics && { w: Number(metrics.width.toFixed(1)), h: Number(metrics.height.toFixed(1)) },
      side: side && { w: Number(side.width.toFixed(1)), h: Number(side.height.toFixed(1)) },
      perf: perf && { w: Number(perf.width.toFixed(1)), h: Number(perf.height.toFixed(1)) },
      overflow: document.documentElement.scrollWidth > window.innerWidth,
    };
  });
  console.log('WIDTH', width, JSON.stringify(data, null, 2));
  await page.close();
}
await browser.close();
