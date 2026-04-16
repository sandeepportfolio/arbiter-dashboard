import fs from 'node:fs';
import path from 'node:path';
import { chromium, devices } from 'playwright';

const outDir = path.resolve('output/playwright');
fs.mkdirSync(outDir, { recursive: true });

const desktop = { viewport: { width: 1440, height: 1320 } };
const tablet = { viewport: { width: 1024, height: 1366 } };
const mobile = devices['iPhone 13'];

const scenarios = [
  { name: 'desktop-public', url: 'http://127.0.0.1:8090/', context: desktop },
  { name: 'tablet-public', url: 'http://127.0.0.1:8090/', context: tablet },
  { name: 'mobile-public', url: 'http://127.0.0.1:8090/', context: mobile },
  { name: 'desktop-ops-locked', url: 'http://127.0.0.1:8090/?route=%2Fops', context: desktop, opsLocked: true },
];

function shouldIgnoreConsole(message) {
  const text = message.text();
  return text.includes('WebSocket') || text.includes('/ws');
}

async function collectLayoutFacts(page) {
  return page.evaluate(() => {
    const box = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        top: Math.round(rect.top),
        left: Math.round(rect.left),
      };
    };

    const operatorCards = Array.from(document.querySelectorAll('.operator-card'));
    const blotterRows = Array.from(document.querySelectorAll('.blotter-row'));
    const disclosures = Array.from(document.querySelectorAll('[data-dense-disclosure]')).map((panel) => {
      const summary = panel.querySelector('summary');
      const summaryRect = box(summary);
      const body = panel.querySelector('.panel-disclosure-body');
      return {
        key: panel.getAttribute('data-dense-disclosure'),
        open: panel.open,
        summaryVisible: Boolean(summaryRect && summaryRect.width > 0 && summaryRect.height > 0),
        summaryRect,
        bodyVisible: Boolean(panel.open),
      };
    });

    return {
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      hasHorizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      performanceSection: box(document.querySelector('#performanceSection')),
      riskSection: box(document.querySelector('#riskSection')),
      opportunitiesSection: box(document.querySelector('#opportunitiesSection')),
      infraSection: box(document.querySelector('#infraSection')),
      operatorCardOverflowCount: operatorCards.filter((card) => card.scrollWidth > card.clientWidth + 1).length,
      operatorCardOverflowSamples: operatorCards
        .filter((card) => card.scrollWidth > card.clientWidth + 1)
        .slice(0, 3)
        .map((card) => card.getAttribute('data-manual-id') || card.getAttribute('data-mapping-id') || card.textContent?.trim().slice(0, 48) || 'card'),
      blotterOverflowCount: blotterRows.filter((row) => row.scrollWidth > row.clientWidth + 1).length,
      denseDisclosureState: disclosures,
      authOverlayVisible: Boolean(document.querySelector('#authOverlay') && !document.querySelector('#authOverlay').classList.contains('hidden')),
    };
  });
}

async function verifyDisclosureStability(page) {
  return page.evaluate(() => {
    const panel = document.querySelector('[data-dense-disclosure]');
    const summary = panel?.querySelector('summary');
    if (!panel || !summary) {
      return {
        found: false,
      };
    }

    const snapshot = () => {
      const rect = summary.getBoundingClientRect();
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        top: Math.round(rect.top),
        left: Math.round(rect.left),
      };
    };

    const clickSummary = () => {
      summary.click();
    };

    const initial = snapshot();
    const initialOpen = panel.open;
    clickSummary();
    const collapsed = snapshot();
    const collapsedOpen = panel.open;
    const collapsedSummaryVisible = collapsed.width > 0 && collapsed.height > 0;
    clickSummary();
    const reopened = snapshot();
    const reopenedOpen = panel.open;

    return {
      found: true,
      initialOpen,
      collapsedOpen,
      reopenedOpen,
      collapsedSummaryVisible,
      initial,
      collapsed,
      reopened,
      stableGeometry: initial.width === collapsed.width && initial.height === collapsed.height,
      restoredGeometry: initial.width === reopened.width && initial.height === reopened.height,
    };
  });
}

async function captureScenario(browser, scenario) {
  const errors = [];
  const context = await browser.newContext(scenario.context);
  const page = await context.newPage();

  page.on('console', (message) => {
    if (message.type() === 'error' && !shouldIgnoreConsole(message)) {
      errors.push(`console:${message.text()}`);
    }
  });
  page.on('pageerror', (error) => errors.push(`pageerror:${error.message}`));
  page.on('requestfailed', (request) => {
    const url = request.url();
    if (!url.includes('/ws')) {
      errors.push(`requestfailed:${request.method()} ${url}`);
    }
  });

  await page.goto(scenario.url, { waitUntil: 'networkidle' });
  await page.waitForSelector('#performanceSection');
  await page.waitForSelector('#riskSection');
  await page.waitForTimeout(1200);

  if (scenario.opsLocked) {
    await page.waitForSelector('#authOverlay:not(.hidden)');
  }

  const disclosureAudit = await verifyDisclosureStability(page);
  const facts = await collectLayoutFacts(page);

  const shotPath = path.join(outDir, `${scenario.name}.png`);
  await page.screenshot({ path: shotPath, fullPage: true });

  await context.close();
  return {
    name: scenario.name,
    screenshot: shotPath,
    errors,
    facts,
    disclosureAudit,
  };
}

const browser = await chromium.launch({ headless: true });
const results = [];
for (const scenario of scenarios) {
  results.push(await captureScenario(browser, scenario));
}
await browser.close();

fs.writeFileSync(path.join(outDir, 'verification-report.json'), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
