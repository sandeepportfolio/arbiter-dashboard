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
    await page.waitForSelector('#authOverlay:not(.hidden)');
    await page.fill('#authEmail', 'sparx.sandeep@gmail.com');
    await page.fill('#authPassword', 'saibaba');
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
    const overlap = (a, b) => {
      if (!a || !b) return false;
      return !(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top);
    };

    const measureTextNode = (el) => {
      if (!el) return null;
      return {
        text: el.textContent?.replace(/\s+/g, ' ').trim() || '',
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
      };
    };

    return {
      hasHorizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      chartGaps: [
        ['#equityChartMeta', '#equityChart', 'equity'],
        ['#edgeChartMeta', '#edgeChart', 'scanner'],
      ].map(([metaSelector, chartSelector, label]) => {
        const meta = document.querySelector(metaSelector);
        const chart = document.querySelector(chartSelector);
        const metaRect = rectData(meta);
        const chartRect = rectData(chart);
        return {
          label,
          gap: metaRect && chartRect ? chartRect.top - metaRect.bottom : null,
        };
      }),
      chartPills: [...document.querySelectorAll('#equityChartMeta .chart-meta-pill, #edgeChartMeta .chart-meta-pill')].map((el) => ({
        text: el.textContent.replace(/\s+/g, ' ').trim(),
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
        hasValue: !!el.querySelector('.chart-meta-value'),
        hasLabel: !!el.querySelector('.chart-meta-label'),
      })),
      panelHeaders: [...document.querySelectorAll('#performanceSection .panel-header, #opportunitiesSection .panel-header, #riskSection .panel-header')].map((el) => ({
        text: el.textContent.replace(/\s+/g, ' ').trim(),
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
      })),
      recentTradeCards: [...document.querySelectorAll('.trade-spotlight-card')].map((el) => ({
        text: el.textContent.replace(/\s+/g, ' ').trim(),
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
      })),
      blotterRows: [...document.querySelectorAll('#opportunityList .blotter-row')].slice(0, 4).map((row) => {
        const title = row.querySelector('.blotter-row-titleblock');
        const metrics = row.querySelector('.blotter-row-metrics');
        const side = row.querySelector('.blotter-row-side');
        const status = side?.querySelector('span');
        const chips = side?.querySelector('.blotter-chip-row');
        return {
          title: row.querySelector('.blotter-row-title')?.textContent?.trim() || '',
          clientWidth: row.clientWidth,
          scrollWidth: row.scrollWidth,
          rowMain: measureTextNode(row.querySelector('.blotter-row-main')),
          titleMetricsOverlap: overlap(rectData(title), rectData(metrics)),
          titleSideOverlap: overlap(rectData(title), rectData(side)),
          metricsStatusOverlap: overlap(rectData(metrics), rectData(status)),
          metricsChipsOverlap: overlap(rectData(metrics), rectData(chips)),
          titleBeforeMetrics: !title || !metrics || rectData(title).bottom <= rectData(metrics).top + 2,
          metricsBeforeSide: !metrics || !side || rectData(metrics).bottom <= rectData(side).top + 8,
        };
      }),
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
  for (const gap of audit.chartGaps) {
    if (gap.gap == null || gap.gap < 10) {
      failures.push(`${name}: ${gap.label} chart spacing is too tight above the graph.`);
    }
  }
  for (const pill of audit.chartPills) {
    if (!pill.hasValue || !pill.hasLabel) {
      failures.push(`${name}: chart pill is missing structured label/value markup: ${pill.text}`);
    }
    if (pill.scrollWidth > pill.clientWidth + 1 || pill.scrollHeight > pill.clientHeight + 1) {
      failures.push(`${name}: chart pill overflows: ${pill.text}`);
    }
  }
  for (const header of audit.panelHeaders) {
    if (header.scrollWidth > header.clientWidth + 1 || header.scrollHeight > header.clientHeight + 4) {
      failures.push(`${name}: panel header overflows: ${header.text}`);
    }
  }
  for (const card of audit.recentTradeCards) {
    if (card.scrollWidth > card.clientWidth + 1 || card.scrollHeight > card.clientHeight + 1) {
      failures.push(`${name}: recent trade card overflows: ${card.text}`);
    }
  }
  for (const row of audit.blotterRows) {
    if (row.scrollWidth > row.clientWidth + 1) {
      failures.push(`${name}: blotter row overflows: ${row.title}`);
    }
    if (row.rowMain && row.rowMain.scrollWidth > row.rowMain.clientWidth + 1) {
      failures.push(`${name}: blotter row main section overflows: ${row.title}`);
    }
    if (row.titleMetricsOverlap || row.titleSideOverlap || row.metricsStatusOverlap || row.metricsChipsOverlap) {
      failures.push(`${name}: blotter row has internal overlap: ${row.title}`);
    }
    if (!row.titleBeforeMetrics || !row.metricsBeforeSide) {
      failures.push(`${name}: blotter row has invalid vertical ordering: ${row.title}`);
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
  for (const card of audit.mobileDisclosures) {
    if (card.scrollWidth > card.clientWidth + 1 || card.scrollHeight > card.clientHeight + 1) {
      failures.push(`${name}: mobile disclosure card overflows: ${card.text}`);
    }
  }
  if (audit.heroStatus && audit.heroStatus.scrollWidth > audit.heroStatus.clientWidth + 1) {
    failures.push(`${name}: hero status row overflows horizontally.`);
  }
  if (!audit.firstLogEntry || audit.firstLogEntry.clientHeight < 100) {
    failures.push(`${name}: Activity Atlas entry height collapsed.`);
  }
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}

console.log('Dashboard polish checks passed.');
