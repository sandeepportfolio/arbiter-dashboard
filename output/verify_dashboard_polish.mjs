import { chromium, devices } from 'playwright';

function rectData(node) {
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  return {
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    left: rect.left,
    width: rect.width,
    height: rect.height,
  };
}

function rectDelta(before, after) {
  if (!before || !after) return null;
  return {
    top: after.top - before.top,
    right: after.right - before.right,
    bottom: after.bottom - before.bottom,
    left: after.left - before.left,
    width: after.width - before.width,
    height: after.height - before.height,
  };
}

async function hoverRects(page, selector) {
  const locator = page.locator(selector);
  const count = await locator.count();
  const checks = [];
  for (let index = 0; index < count; index += 1) {
    const item = locator.nth(index);
    if (!(await item.isVisible())) continue;
    const before = await item.boundingBox();
    if (!before) continue;
    const text = (await item.textContent())?.replace(/\s+/g, ' ').trim() || '';
    await item.hover({ force: true });
    await page.waitForTimeout(80);
    const after = await item.boundingBox();
    await page.mouse.move(0, 0);
    checks.push({ selector, index, text, before, after });
  }
  return checks;
}

async function auditScenario(browser, scenario) {
  const context = await browser.newContext(scenario.context);
  const page = await context.newPage();
  await page.goto(scenario.url, { waitUntil: 'networkidle' });

  if (scenario.opsAuth) {
    const authEmail = process.env.UI_USER_EMAIL || 'operator@arbiter.local';
    const authPassword = process.env.UI_USER_PASSWORD || 'secret';
    await page.waitForSelector('#authOverlay:not(.hidden)');
    await page.fill('#authEmail', authEmail);
    await page.fill('#authPassword', authPassword);
    await page.click('#authSubmit');
    await page.waitForFunction(() => document.querySelector('#authOverlay')?.classList.contains('hidden'));
  }

  await page.waitForTimeout(900);
  const hoverSelectors = ['.filter-pill', '.action-button', '.log-filter-chip'];
  const hoverChecks = [];
  for (const selector of hoverSelectors) {
    hoverChecks.push(...await hoverRects(page, selector));
  }

  const result = await page.evaluate((rectDataFn) => {
    const rectData = eval(`(${rectDataFn})`);
    const atlas = document.querySelector('#logCategoryAtlas');
    const atlasParent = document.querySelector('.log-atlas');
    if (atlas) {
      atlas.scrollTop = atlas.scrollHeight;
    }
    const atlasLastCard = atlas?.lastElementChild;
    const opportunityPanel = document.querySelector('#opportunitiesSection');
    const opportunityList = document.querySelector('#opportunityList');
    const scannerPanel = document.querySelector('.scanner-performance-panel');

    return {
      hasHorizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      operatorCards: [...document.querySelectorAll('#manualQueue .operator-card, #incidentList .operator-card, #mappingList .operator-card')].map((card) => {
        const cardRect = rectData(card);
        const actionButtons = [...card.querySelectorAll('.action-button')].map((button) => {
          const buttonRect = rectData(button);
          const buttonOverflow = !!cardRect && !!buttonRect && (
            buttonRect.left < cardRect.left - 1 ||
            buttonRect.right > cardRect.right + 1 ||
            buttonRect.top < cardRect.top - 1 ||
            buttonRect.bottom > cardRect.bottom + 1
          );
          return {
            text: button.textContent.replace(/\s+/g, ' ').trim(),
            clientWidth: button.clientWidth,
            scrollWidth: button.scrollWidth,
            clientHeight: button.clientHeight,
            scrollHeight: button.scrollHeight,
            buttonOverflow,
          };
        });
        return {
          id: card.getAttribute('data-manual-id') || card.getAttribute('data-incident-id') || card.getAttribute('data-mapping-id') || '',
          text: card.textContent.replace(/\s+/g, ' ').trim(),
          clientWidth: card.clientWidth,
          scrollWidth: card.scrollWidth,
          clientHeight: card.clientHeight,
          scrollHeight: card.scrollHeight,
          cardOverflow: card.scrollWidth > card.clientWidth + 1 || card.scrollHeight > card.clientHeight + 1,
          buttonOverflow: actionButtons.some((button) => button.buttonOverflow || button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1),
          actionButtons,
        };
      }),
      mobileDisclosures: [...document.querySelectorAll('.mobile-disclosure-card')].map((el) => ({
        text: el.textContent.replace(/\s+/g, ' ').trim(),
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
      })),
      heroStatus: (() => {
        const el = document.querySelector('.hero-status');
        return el ? { clientWidth: el.clientWidth, scrollWidth: el.scrollWidth } : null;
      })(),
      firstLogEntry: (() => {
        const entry = document.querySelector('.log-entry');
        return entry ? { clientHeight: entry.clientHeight, scrollHeight: entry.scrollHeight } : null;
      })(),
      categoryAtlas: atlas ? {
        overflowY: getComputedStyle(atlas).overflowY,
        clientHeight: atlas.clientHeight,
        scrollHeight: atlas.scrollHeight,
        atlasRect: rectData(atlas),
        parentRect: rectData(atlasParent),
        lastCardRect: rectData(atlasLastCard),
      } : null,
      opportunityBlotter: opportunityList ? {
        overflowY: getComputedStyle(opportunityList).overflowY,
        clientHeight: opportunityList.clientHeight,
        scrollHeight: opportunityList.scrollHeight,
        listRect: rectData(opportunityList),
        panelRect: rectData(opportunityPanel),
        scannerRect: rectData(scannerPanel),
      } : null,
    };
  }, rectData.toString());

  result.hoverChecks = hoverChecks;
  await context.close();
  return result;
}

const scenarios = [
  {
    name: 'desktop-public-wide',
    url: 'http://127.0.0.1:8090/',
    context: { viewport: { width: 1600, height: 1200 } },
  },
  {
    name: 'desktop-public-compact',
    url: 'http://127.0.0.1:8090/',
    context: { viewport: { width: 1280, height: 1200 } },
  },
  {
    name: 'desktop-ops-authenticated',
    url: 'http://127.0.0.1:8090/?route=%2Fops',
    context: { viewport: { width: 1440, height: 1200 } },
    opsAuth: true,
  },
  {
    name: 'mobile-public',
    url: 'http://127.0.0.1:8090/',
    context: devices['iPhone 13'],
  },
];

const browser = await chromium.launch({ headless: true });
const results = [];
for (const scenario of scenarios) {
  results.push({ name: scenario.name, audit: await auditScenario(browser, scenario) });
}
await browser.close();

const failures = [];
for (const result of results) {
  const { name, audit } = result;
  if (audit.hasHorizontalOverflow) failures.push(`${name}: document overflows horizontally.`);
  if (audit.categoryAtlas) {
    const { parentRect, lastCardRect, scrollHeight, clientHeight } = audit.categoryAtlas;
    if (scrollHeight > clientHeight + 1 && parentRect && lastCardRect && lastCardRect.bottom > parentRect.bottom + 1) {
      failures.push(`${name}: category atlas clips the last category card inside the sticky rail.`);
    }
  }
  if (audit.opportunityBlotter) {
    const { overflowY, panelRect, scannerRect } = audit.opportunityBlotter;
    if (!['auto', 'scroll'].includes(overflowY)) {
      failures.push(`${name}: live trade candidates list is not internally scrollable.`);
    }
    if (panelRect && scannerRect && Math.abs(panelRect.height - scannerRect.height) > 24) {
      failures.push(`${name}: live trade candidates panel height drifts from the scanner chart panel.`);
    }
  }
  for (const hoverCheck of audit.hoverChecks || []) {
    const delta = rectDelta(hoverCheck.before, hoverCheck.after);
    if (!delta) {
      failures.push(`${name}: ${hoverCheck.selector} hover target could not be measured: ${hoverCheck.text || hoverCheck.index}.`);
      continue;
    }
    const moved = Object.values(delta).some((value) => Math.abs(value) > 0.5);
    if (moved) {
      failures.push(`${name}: ${hoverCheck.selector} hover geometry drifted: ${hoverCheck.text || hoverCheck.index} ${JSON.stringify(delta)}`);
    }
  }
  for (const card of audit.operatorCards || []) {
    if (!card.actionButtons || card.actionButtons.length === 0) {
      continue;
    }
    if (card.cardOverflow) {
      failures.push(`${name}: operator card scroll overflow: ${card.id || card.text}`);
    }
    if (card.buttonOverflow) {
      failures.push(`${name}: operator action button overflow: ${card.id || card.text}`);
    }
    for (const button of card.actionButtons || []) {
      if (button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1) {
        failures.push(`${name}: operator button truncation: ${button.text}`);
      }
    }
  }
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}

console.log('Dashboard polish checks passed.');
