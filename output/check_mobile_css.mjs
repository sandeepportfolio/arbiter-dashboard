import { chromium, devices } from 'playwright';
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext(devices['iPhone 13']);
const page = await context.newPage();
await page.goto('http://127.0.0.1:8090/', { waitUntil: 'networkidle' });
const result = await page.evaluate(() => ({
  width: window.innerWidth,
  nav: getComputedStyle(document.querySelector('.primary-nav')).display,
  range: getComputedStyle(document.querySelector('.range-pills')).display,
  status: getComputedStyle(document.querySelector('.status-band-overview')).display,
}));
console.log(JSON.stringify(result));
await browser.close();
