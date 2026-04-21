import { chromium, devices } from 'playwright';

function cssPath(el) {
  if (!(el instanceof Element)) return '';
  const parts = [];
  while (el && el.nodeType === 1 && parts.length < 4) {
    let part = el.tagName.toLowerCase();
    if (el.id) {
      part += `#${el.id}`;
      parts.unshift(part);
      break;
    }
    if (el.classList.length) {
      part += '.' + [...el.classList].slice(0, 2).join('.');
    }
    parts.unshift(part);
    el = el.parentElement;
  }
  return parts.join(' > ');
}

async function audit(page, name) {
  const issues = await page.evaluate(({ cssPathFn }) => {
    const cssPath = eval(`(${cssPathFn})`);
    const nodes = [...document.querySelectorAll('body *')];
    return nodes
      .filter((el) => {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        const rect = el.getBoundingClientRect();
        if (rect.width < 8 || rect.height < 8) return false;
        if (!el.textContent?.trim()) return false;
        const clippedX = el.scrollWidth - el.clientWidth > 2;
        const clippedY = el.scrollHeight - el.clientHeight > 2;
        if (!clippedX && !clippedY) return false;
        const overflowX = style.overflowX;
        const overflowY = style.overflowY;
        const hidesX = ['hidden', 'clip', 'auto', 'scroll'].includes(overflowX);
        const hidesY = ['hidden', 'clip', 'auto', 'scroll'].includes(overflowY);
        return (clippedX && hidesX) || (clippedY && hidesY) || clippedX;
      })
      .slice(0, 120)
      .map((el) => ({
        path: cssPath(el),
        text: el.textContent.replace(/\s+/g, ' ').trim().slice(0, 120),
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
        overflowX: getComputedStyle(el).overflowX,
        overflowY: getComputedStyle(el).overflowY,
      }));
  }, { cssPathFn: cssPath.toString() });
  return { name, issues };
}

const browser = await chromium.launch({ headless: true });
const results = [];

const desktop = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
const desktopPage = await desktop.newPage();
await desktopPage.goto('http://127.0.0.1:8090/?route=%2Fops', { waitUntil: 'networkidle' });
await desktopPage.fill('#authEmail', 'operator@arbiter.local');
await desktopPage.fill('#authPassword', 'secret');
await desktopPage.click('#authSubmit');
await desktopPage.waitForFunction(() => document.querySelector('#authOverlay')?.classList.contains('hidden'));
await desktopPage.waitForTimeout(900);
results.push(await audit(desktopPage, 'desktop-ops'));
await desktop.close();

const mobile = await browser.newContext(devices['iPhone 13']);
const mobilePage = await mobile.newPage();
await mobilePage.goto('http://127.0.0.1:8090/', { waitUntil: 'networkidle' });
await mobilePage.waitForTimeout(900);
results.push(await audit(mobilePage, 'mobile-public'));
await mobile.close();

await browser.close();
console.log(JSON.stringify(results, null, 2));
