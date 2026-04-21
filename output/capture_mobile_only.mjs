import { chromium, devices } from 'playwright';
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext(devices['iPhone 13']);
const page = await context.newPage();
await page.goto('http://127.0.0.1:8090/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
await page.screenshot({ path: 'output/playwright/mobile-public-top-v2.png' });
await browser.close();
