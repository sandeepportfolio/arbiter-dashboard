/**
 * Kalshi demo API key creation using pasted browser cookies.
 *
 * Headless Chromium, ephemeral context (no persistent profile).
 * Reads cookies from C:/Users/sande/AppData/Local/Temp/kalshi_cookies.json,
 * injects them BEFORE any navigation, then drives the api-keys flow.
 *
 * Outputs:
 *   keys/kalshi_demo_private.pem
 *   /tmp/kalshi_key_id.txt
 */
import { chromium } from 'playwright';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

const REPO = 'C:/Users/sande/Documents/arbiter-dashboard';
const KEYS_DIR = path.join(REPO, 'keys');
const PEM_TARGET = path.join(KEYS_DIR, 'kalshi_demo_private.pem');
const UUID_OUT = '/tmp/kalshi_key_id.txt';
const UUID_OUT_FALLBACK = path.join(os.tmpdir(), 'kalshi_key_id.txt');
const COOKIES_PATH = 'C:/Users/sande/AppData/Local/Temp/kalshi_cookies.json';
const KEY_NAME = 'arbiter-automation-20260420';
const URL = 'https://demo.kalshi.co/account/api-keys';

fs.mkdirSync(KEYS_DIR, { recursive: true });

function log(...args) {
  console.log('[kalshi-cookies]', ...args);
}

async function findFirstVisible(page, selectors, timeoutMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    for (const sel of selectors) {
      try {
        const loc = page.locator(sel).first();
        if (await loc.isVisible({ timeout: 200 })) return loc;
      } catch (_) { /* ignore */ }
    }
    await page.waitForTimeout(200);
  }
  return null;
}

function writeUuid(uuid) {
  try {
    fs.writeFileSync(UUID_OUT, uuid, 'utf8');
  } catch (err) {
    // On Windows, /tmp may not exist — fall back to os.tmpdir()
    log('warn: failed to write', UUID_OUT, '-', err.message, '-> falling back');
    fs.writeFileSync(UUID_OUT_FALLBACK, uuid, 'utf8');
  }
}

async function main() {
  if (!fs.existsSync(COOKIES_PATH)) {
    log('ERROR: cookie file not found at', COOKIES_PATH);
    process.exit(10);
  }

  const rawCookies = JSON.parse(fs.readFileSync(COOKIES_PATH, 'utf8'));
  // Sanity: an array of cookie-shaped records. Do not log contents.
  if (!Array.isArray(rawCookies) || rawCookies.length === 0) {
    log('ERROR: cookie file did not parse to a non-empty array.');
    process.exit(11);
  }
  log('loaded', rawCookies.length, 'cookies (contents suppressed)');

  // Normalize cookies for Playwright addCookies().
  // Chromium rule: sameSite=None requires secure=true. If secure is false,
  // downgrade sameSite to 'Lax' so the cookie isn't silently rejected.
  const normalized = rawCookies.map((c) => {
    const out = { ...c };
    if (out.sameSite) {
      const s = String(out.sameSite).toLowerCase();
      if (s === 'no_restriction' || s === 'none' || s === 'unspecified') out.sameSite = 'None';
      else if (s === 'lax') out.sameSite = 'Lax';
      else if (s === 'strict') out.sameSite = 'Strict';
      else delete out.sameSite;
    }
    if (out.sameSite === 'None' && out.secure !== true) {
      out.sameSite = 'Lax';
    }
    delete out.hostOnly;
    delete out.session;
    delete out.storeId;
    delete out.id;
    if (!out.domain && !out.url) {
      out.domain = '.kalshi.co';
    }
    if (!out.path) out.path = '/';
    return out;
  });

  log('launching headless chromium (ephemeral context)');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1400, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  });

  await context.addCookies(normalized);
  // Diagnostic: list cookie NAMES that made it into the context (no values)
  const post = await context.cookies('https://demo.kalshi.co/');
  log('cookies now in context for demo.kalshi.co:', post.map(c => c.name).join(',') || '(none)');
  log('injected names:', normalized.map(c => c.name).join(','));
  log('cookies injected into context');

  const page = await context.newPage();
  page.setDefaultTimeout(20000);

  // Diagnostic: log every response to auth/user endpoints
  page.on('response', async (resp) => {
    try {
      const url = resp.url();
      if (url.includes('/trade-api/v2/') || url.includes('/sign-in') || url.includes('api-key') || url.includes('/user/') || url.includes('/session')) {
        log('[resp]', resp.status(), url.slice(0, 140));
      }
    } catch (_) {}
  });
  page.on('requestfailed', (req) => {
    log('[reqfail]', req.failure()?.errorText, req.url().slice(0, 140));
  });

  // First hit the root so the SPA hydrates any CSRF / auxiliary cookies
  // before we go to the protected page (which otherwise client-side
  // redirects to /sign-in before the cookies "take").
  log('warming up session via demo.kalshi.co root...');
  try {
    await page.goto('https://demo.kalshi.co/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  } catch (err) {
    log('warn: warmup goto had issue:', err.message);
  }
  await page.waitForTimeout(3000);

  log('navigating to', URL, '(networkidle)...');
  try {
    await page.goto(URL, { waitUntil: 'networkidle', timeout: 45000 });
  } catch (err) {
    log('warn: networkidle wait timed out, continuing anyway:', err.message);
  }
  await page.waitForTimeout(3000);

  // Check auth state: presence of Log in / Sign up buttons means cookies were rejected.
  const loginBtnCount = await page.locator('button:has-text("Log in")').count().catch(() => 0);
  const signUpBtnCount = await page.locator('button:has-text("Sign up")').count().catch(() => 0);
  log(`auth check: Log in btn=${loginBtnCount} Sign up btn=${signUpBtnCount} url=${page.url()}`);
  if (loginBtnCount > 0 || signUpBtnCount > 0) {
    log('ERROR: page still shows login UI — cookies appear rejected/expired.');
    const htmlPath = path.join(os.tmpdir(), 'kalshi_cookie_rejected.html');
    fs.writeFileSync(htmlPath, await page.content().catch(() => ''), 'utf8');
    log('page HTML dumped to', htmlPath);
    await context.close();
    await browser.close();
    process.exit(20);
  }

  log('session appears valid; searching for Create button...');
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
    log('ERROR: could not locate Create button.');
    const dumpPath = path.join(os.tmpdir(), 'kalshi_api_keys_nocreate.html');
    fs.writeFileSync(dumpPath, await page.content().catch(() => ''), 'utf8');
    log('page HTML dumped to', dumpPath);
    const btnTexts = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('button,a,[role="button"]'));
      return els.map(e => ({
        tag: e.tagName,
        text: (e.innerText || '').trim().slice(0, 80),
        testid: e.getAttribute('data-testid') || '',
      })).filter(x => x.text);
    }).catch(() => []);
    fs.writeFileSync(path.join(os.tmpdir(), 'kalshi_buttons_cookies.json'), JSON.stringify(btnTexts, null, 2), 'utf8');
    log('visible buttons dumped to', path.join(os.tmpdir(), 'kalshi_buttons_cookies.json'));
    await context.close();
    await browser.close();
    process.exit(30);
  }

  // Pre-register download + response listeners
  const downloadPromise = page.waitForEvent('download', { timeout: 15000 }).catch(() => null);
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

  log('waiting for PEM download (5s)...');
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

  // UUID extraction
  let keyId = null;
  for (const r of apiKeyResponses) {
    const body = r.body;
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
    const idLike = candidates.find(c => /id/i.test(c.key)) || candidates[0];
    if (idLike) { keyId = idLike.val; break; }
  }

  if (!keyId) {
    await page.waitForTimeout(1500);
    const bodyText = await page.locator('body').innerText().catch(() => '');
    const m = bodyText.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (m) keyId = m[0];
  }

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
    writeUuid(keyId);
    // Do not log full UUID; log prefix only
    log('key UUID captured (prefix):', keyId.slice(0, 8) + '-...');
  } else {
    log('ERROR: could not extract key UUID.');
    const dumpPath = path.join(os.tmpdir(), 'kalshi_post_create_cookies.html');
    fs.writeFileSync(dumpPath, await page.content().catch(() => ''), 'utf8');
    log('post-create page HTML dumped to', dumpPath);
  }

  log('SUMMARY:');
  log('  PEM exists:', fs.existsSync(PEM_TARGET), '->', PEM_TARGET);
  log('  UUID file exists:', fs.existsSync(UUID_OUT) || fs.existsSync(UUID_OUT_FALLBACK));
  log('  UUID prefix:', keyId ? (keyId.slice(0, 8) + '-...') : '(not captured)');

  await context.close();
  await browser.close();

  if (!pemWritten || !keyId) {
    process.exit(4);
  }
}

main().catch(err => {
  console.error('[kalshi-cookies] FATAL:', err && err.message ? err.message : err);
  process.exit(1);
});
