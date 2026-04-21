/**
 * Kalshi demo API key setup via headed Playwright.
 *
 * Launches a visible Chromium window at demo.kalshi.co/account/api-keys.
 * Operator logs in manually; script then creates an API key, downloads the
 * PEM, and captures the UUID.
 */
import { chromium } from 'playwright';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

const REPO = 'C:/Users/sande/Documents/arbiter-dashboard';
const KEYS_DIR = path.join(REPO, 'keys');
const PROFILE_DIR = path.join(REPO, '.playwright-kalshi');
const PEM_TARGET = path.join(KEYS_DIR, 'kalshi_demo_private.pem');
const UUID_OUT = path.join(os.tmpdir(), 'kalshi_key_id.txt');
const KEY_NAME = 'arbiter-automation-20260420';
const URL = 'https://demo.kalshi.co/account/api-keys';

fs.mkdirSync(KEYS_DIR, { recursive: true });
fs.mkdirSync(PROFILE_DIR, { recursive: true });

function log(...args) {
  console.log('[kalshi-setup]', ...args);
}

async function findFirstVisible(page, selectors, timeoutMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    for (const sel of selectors) {
      try {
        const loc = page.locator(sel).first();
        if (await loc.isVisible({ timeout: 250 })) return loc;
      } catch (_) { /* ignore */ }
    }
    await page.waitForTimeout(200);
  }
  return null;
}

async function main() {
  log('launching headed chromium with persistent profile:', PROFILE_DIR);
  const context = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: false,
    acceptDownloads: true,
    viewport: { width: 1400, height: 900 },
    args: ['--disable-blink-features=AutomationControlled'],
  });

  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(15000);

  log('navigating to', URL);
  await page.goto(URL, { waitUntil: 'domcontentloaded' });

  // Wait for login. Strict detection: authenticated api-keys page MUST NOT have
  // an email/password input (which would indicate a login form) AND must be on
  // the /account/api-keys URL with an API-key-related heading visible.
  log('======================================================================');
  log('ACTION REQUIRED: Log into demo.kalshi.co in the visible browser window.');
  log('The script will auto-detect once you reach the API keys page.');
  log('(waiting up to 10 minutes)');
  log('======================================================================');
  const loginDeadline = Date.now() + 10 * 60 * 1000;
  let loggedIn = false;
  let lastStatusAt = 0;
  let stableOnTargetSince = 0; // ms-ts of first tick we saw /account/api-keys with no login inputs
  const STABLE_MS = 3000; // consider logged in after 3s on target URL with no login inputs
  while (Date.now() < loginDeadline) {
    const url = page.url();
    const pwdCount = await page.locator('input[type="password"]').count().catch(() => 1);
    const emailCount = await page.locator('input[type="email"], input[name="email" i]').count().catch(() => 1);
    const isLoginForm = (pwdCount > 0) || (emailCount > 0);
    const onTarget = url.includes('/account/api-keys') && !isLoginForm;

    if (Date.now() - lastStatusAt > 15000) {
      log(`status: url=${url} pwd_inputs=${pwdCount} email_inputs=${emailCount} onTarget=${onTarget}`);
      lastStatusAt = Date.now();
    }

    if (onTarget) {
      // Kalshi shows "Log in"/"Sign up" buttons in the top-right when NOT authenticated,
      // even on /account/api-keys (it just renders as a marketing page). Use .count() which
      // doesn't depend on the element being in the current viewport.
      const loginBtnCount = await page.locator('button:has-text("Log in")').count().catch(() => 1);
      const signUpBtnCount = await page.locator('button:has-text("Sign up")').count().catch(() => 1);
      const anyLoginBtn = loginBtnCount > 0 || signUpBtnCount > 0;

      if (anyLoginBtn) {
        if (Date.now() - lastStatusAt > 14000) {
          log(`  -> still seeing Log in=${loginBtnCount} Sign up=${signUpBtnCount} — Kalshi session not established yet.`);
        }
        stableOnTargetSince = 0;
      } else {
        if (stableOnTargetSince === 0) {
          log('no "Log in"/"Sign up" buttons detected — Kalshi session likely established.');
          stableOnTargetSince = Date.now();
        }
        // Positive auth signal: any API-key-ish heading or button, OR page text mentions "API keys".
        const authSignal = await findFirstVisible(page, [
          'h1:has-text("API")',
          'h2:has-text("API")',
          'h3:has-text("API")',
          'h4:has-text("API")',
          'text=/API Keys?/i',
          'text=/Create.{0,20}API.{0,10}[Kk]ey/',
          'text=/New.{0,10}API.{0,10}[Kk]ey/',
          'button:has-text("Create API")',
          'button:has-text("New API")',
          'button:has-text("Generate")',
        ], 1000);
        if (authSignal) {
          loggedIn = true;
          break;
        }
        // Fallback: stable on target URL long enough with no login button
        if (Date.now() - stableOnTargetSince > STABLE_MS) {
          log('URL stable on /account/api-keys with no Log in/Sign up buttons; treating as logged in.');
          loggedIn = true;
          break;
        }
      }
    } else {
      stableOnTargetSince = 0;
    }
    await page.waitForTimeout(1500);
  }

  if (!loggedIn) {
    log('ERROR: login window elapsed without reaching authenticated /account/api-keys.');
    const html = await page.content().catch(() => '');
    const dumpPath = path.join(os.tmpdir(), 'kalshi_api_keys_page.html');
    fs.writeFileSync(dumpPath, html, 'utf8');
    log('page HTML dumped to', dumpPath);
    log('page URL:', page.url());
    log('leaving browser open for 10 more seconds for inspection...');
    await page.waitForTimeout(10000);
    await context.close();
    process.exit(2);
  }

  log('logged-in UI detected; locating Create button (allowing up to 30s for SPA hydration)...');
  // Give the SPA a moment to finish rendering before scanning for buttons
  await page.waitForTimeout(2000);
  const createBtn = await findFirstVisible(page, [
    'button:has-text("Create API key")',
    'button:has-text("New API key")',
    'button:has-text("Add API key")',
    'button:has-text("Create key")',
    'button:has-text("New key")',
    'button:has-text("Generate key")',
    'button:has-text("Generate")',
    'button:has-text("Create")',
    'button:has-text("New")',
    '[data-testid*="create" i]',
    '[data-testid*="api-key" i]',
    'a:has-text("Create API key")',
    'a:has-text("New API key")',
  ], 30000);
  if (!createBtn) {
    log('ERROR: could not relocate Create button after login detect.');
    const dumpPath = path.join(os.tmpdir(), 'kalshi_api_keys_page_postlogin.html');
    const html = await page.content().catch(() => '');
    fs.writeFileSync(dumpPath, html, 'utf8');
    log('page HTML dumped to', dumpPath);
    // Also dump all visible button/anchor text for diagnostic
    const btnTexts = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('button,a,[role="button"]'));
      return els.map(e => ({
        tag: e.tagName,
        text: (e.innerText || '').trim().slice(0, 80),
        testid: e.getAttribute('data-testid') || '',
      })).filter(x => x.text);
    }).catch(() => []);
    fs.writeFileSync(path.join(os.tmpdir(), 'kalshi_buttons.json'), JSON.stringify(btnTexts, null, 2), 'utf8');
    log('visible buttons dumped to', path.join(os.tmpdir(), 'kalshi_buttons.json'));
    log('leaving browser open 30s so you can see the page state...');
    await page.waitForTimeout(30000);
    await context.close();
    process.exit(3);
  }

  // Set up download + response listeners BEFORE clicking
  const downloadPromise = page.waitForEvent('download', { timeout: 60000 }).catch(() => null);

  // Also capture API responses that might contain the key_id
  const apiKeyResponses = [];
  page.on('response', async (resp) => {
    try {
      const url = resp.url();
      if (url.includes('api-key') || url.includes('apikey') || url.includes('api_key')) {
        const ct = resp.headers()['content-type'] || '';
        if (ct.includes('application/json')) {
          const body = await resp.json().catch(() => null);
          if (body) apiKeyResponses.push({ url, body });
        }
      }
    } catch (_) { /* ignore */ }
  });

  log('clicking Create button...');
  await createBtn.click();

  // Fill in name field if dialog appears
  const nameField = await findFirstVisible(page, [
    'input[placeholder*="name" i]',
    'input[name="name"]',
    'input[name="label"]',
    'input[id*="name" i]',
    'input[type="text"]',
  ], 5000);
  if (nameField) {
    log('filling key name:', KEY_NAME);
    await nameField.fill(KEY_NAME);
  } else {
    log('NOTE: no name input appeared; proceeding anyway.');
  }

  // Confirm submit button
  const submitBtn = await findFirstVisible(page, [
    'button:has-text("Create"):not(:has-text("Cancel"))',
    'button:has-text("Generate")',
    'button:has-text("Confirm")',
    'button:has-text("Submit")',
    'button[type="submit"]',
  ], 5000);
  if (submitBtn) {
    log('clicking submit button...');
    await submitBtn.click();
  } else {
    log('no secondary submit button found; assuming single-click flow.');
  }

  // Wait for download
  log('waiting for PEM download...');
  const download = await downloadPromise;
  let pemWritten = false;
  if (download) {
    const suggested = download.suggestedFilename();
    log('download started, suggested name:', suggested);
    await download.saveAs(PEM_TARGET);
    log('PEM saved to', PEM_TARGET);
    pemWritten = true;
  } else {
    log('no download event; will try to scrape PEM from DOM.');
  }

  // Try to extract UUID from page or from API responses
  let keyId = null;

  // First from captured API responses
  for (const r of apiKeyResponses) {
    const body = r.body;
    // Common shapes: { api_key: { id: "..." } } or { id: "..." }
    const candidates = [];
    const walk = (obj) => {
      if (!obj || typeof obj !== 'object') return;
      for (const [k, v] of Object.entries(obj)) {
        if (typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v)) {
          candidates.push({ key: k, val: v });
        } else if (typeof v === 'object') {
          walk(v);
        }
      }
    };
    walk(body);
    // Prefer one whose key name contains id
    const idLike = candidates.find(c => /id/i.test(c.key)) || candidates[0];
    if (idLike) { keyId = idLike.val; break; }
  }

  // Fallback: scan page text for a UUID
  if (!keyId) {
    await page.waitForTimeout(1500);
    const bodyText = await page.locator('body').innerText().catch(() => '');
    const m = bodyText.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (m) keyId = m[0];
  }

  // Fallback: scrape PEM content from DOM if download didn't happen
  if (!pemWritten) {
    const bodyText = await page.locator('body').innerText().catch(() => '');
    const pemMatch = bodyText.match(/-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA )?PRIVATE KEY-----/);
    if (pemMatch) {
      fs.writeFileSync(PEM_TARGET, pemMatch[0] + '\n', 'utf8');
      log('PEM scraped from DOM, written to', PEM_TARGET);
      pemWritten = true;
    }
  }

  if (keyId) {
    fs.writeFileSync(UUID_OUT, keyId, 'utf8');
    log('key UUID:', keyId);
    log('UUID written to', UUID_OUT);
  } else {
    log('ERROR: could not extract key UUID from page or API responses.');
    const dumpPath = path.join(os.tmpdir(), 'kalshi_post_create_page.html');
    const html = await page.content().catch(() => '');
    fs.writeFileSync(dumpPath, html, 'utf8');
    log('post-create page HTML dumped to', dumpPath);
  }

  // Summary
  log('SUMMARY:');
  log('  PEM file exists:', fs.existsSync(PEM_TARGET), '->', PEM_TARGET);
  log('  UUID file exists:', fs.existsSync(UUID_OUT), '->', UUID_OUT);
  log('  UUID:', keyId || '(not captured)');

  log('leaving browser open 5s for verification...');
  await page.waitForTimeout(5000);
  await context.close();

  if (!pemWritten || !keyId) {
    process.exit(4);
  }
}

main().catch(err => {
  console.error('[kalshi-setup] FATAL:', err);
  process.exit(1);
});
