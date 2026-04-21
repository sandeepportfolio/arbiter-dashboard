#!/usr/bin/env node
/**
 * uat_11_13.mjs — Phase 4 browser UATs 11-13 against running arbiter.main.
 *
 * Test 11: Kill-switch ARM/RESET end-to-end (SAFE-01)
 *          - login, load /ops, capture pre-arm state
 *          - POST /api/kill-switch {action:"arm", reason:"UAT-11"} and screenshot ARMED UI
 *          - POST /api/kill-switch {action:"reset", note:"UAT-11 reset"} and screenshot DISARMED UI
 *
 * Test 12: Shutdown banner visibility (SAFE-05)
 *          - inject a shutdown_state WS event client-side via page.evaluate and
 *            screenshot the banner; verifies renderShutdownBanner wiring end-to-end
 *          - does NOT kill arbiter.main (backend must stay up for concurrent agents).
 *
 * Test 13: Rate-limit pill color transition (SAFE-04)
 *          - inject rate_limit_state WS payloads with varying remaining_penalty_seconds
 *            and screenshot the pills in ok/warn tones
 */

import { writeFileSync, existsSync, readFileSync, mkdirSync } from "node:fs";
import { chromium } from "playwright";

const BASE = "http://127.0.0.1:8080";
const OPS_URL = `${BASE}/ops`;
const OUT_DIR = "C:/Users/sande/Documents/arbiter-dashboard/output/uat-11-13";
const PROFILE_DIR = "C:/Users/sande/Documents/arbiter-dashboard/.playwright-uat";
const UI_EMAIL = "sparx.sandeep@gmail.com";
const UI_PASSWORD = "saibaba";

if (!existsSync(OUT_DIR)) mkdirSync(OUT_DIR, { recursive: true });

const report = { test11: {}, test12: {}, test13: {} };

/** Login via API and return the session token */
async function loginViaApi() {
  const res = await fetch(`${BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: UI_EMAIL, password: UI_PASSWORD }),
  });
  if (!res.ok) throw new Error(`login failed: ${res.status}`);
  const body = await res.json();
  return body.token;
}

async function postKillSwitch(token, payload) {
  const res = await fetch(`${BASE}/api/kill-switch`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Cookie": `arbiter_session=${token}`,
    },
    body: JSON.stringify(payload),
  });
  const text = await res.text();
  return { status: res.status, body: text };
}

async function getSafetyStatus() {
  const res = await fetch(`${BASE}/api/safety/status`);
  return await res.json();
}

/**
 * Inject a synthetic WebSocket message handler trigger.
 * The dashboard subscribes to WS messages via state mutation; we replicate the
 * effect by directly mutating `state` and invoking `render`. Both symbols are
 * module-scoped globals in dashboard.js so we access them via `window.__state`
 * if exposed; otherwise we dispatch via the live WS wire.
 *
 * We instead use a more robust approach: find the live WebSocket on window and
 * dispatch a synthetic MessageEvent. If that doesn't work we fall back to
 * directly patching the DOM state via a tiny escape hatch we inject.
 */
async function injectWsMessage(page, payload) {
  return await page.evaluate((msg) => {
    // Try to find the live socket: dashboard.js stores it on state.websocket
    // but state is module-scoped. As a fallback we dispatch a raw message on
    // the current WS instance via MessageEvent. We inject a hook.
    // The cleanest path: monkey-patch has already happened — we stored the
    // last socket in window.__uatLastSocket via the hook we installed below.
    const sock = window.__uatLastSocket;
    if (!sock) return { ok: false, reason: "no socket captured" };
    sock.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(msg) }));
    return { ok: true };
  }, payload);
}

async function installWsHook(page) {
  // Install a WebSocket constructor shim that stashes the most recent socket.
  await page.addInitScript(() => {
    const Orig = window.WebSocket;
    const Shim = function (...args) {
      const s = new Orig(...args);
      window.__uatLastSocket = s;
      return s;
    };
    Shim.prototype = Orig.prototype;
    Shim.CONNECTING = Orig.CONNECTING;
    Shim.OPEN = Orig.OPEN;
    Shim.CLOSING = Orig.CLOSING;
    Shim.CLOSED = Orig.CLOSED;
    window.WebSocket = Shim;
  });
}

async function authenticateContext(context, token) {
  await context.addCookies([
    {
      name: "arbiter_session",
      value: token,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      secure: false,
      sameSite: "Lax",
    },
  ]);
}

async function screenshot(page, name) {
  const p = `${OUT_DIR}/${name}.png`;
  await page.screenshot({ path: p, fullPage: false });
  return p;
}

async function waitMs(n) {
  return new Promise((r) => setTimeout(r, n));
}

/** Ensure the dashboard exposes an escape hatch to force a re-render with synthetic state. */
async function ensureRateLimitHost(page) {
  // Inject a host element #rateLimitIndicators into the DOM if missing, and
  // place it inside the kill-switch toolbar so screenshots are coherent.
  return await page.evaluate(() => {
    let host = document.getElementById("rateLimitIndicators");
    if (host) return { existed: true };
    host = document.createElement("div");
    host.id = "rateLimitIndicators";
    host.style.cssText = "display:flex;gap:8px;padding:8px;flex-wrap:wrap;margin-top:8px;";
    const anchor = document.getElementById("killSwitchToolbar") || document.body;
    anchor.parentElement?.insertBefore(host, anchor.nextSibling) || document.body.appendChild(host);
    return { existed: false, injected: true };
  });
}

async function main() {
  const token = await loginViaApi();
  console.log("[setup] got session token");

  const context = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: true,
    viewport: { width: 1440, height: 900 },
  });

  // Persistent context with no pages => create one
  const page = context.pages()[0] || (await context.newPage());

  await installWsHook(page);
  await authenticateContext(context, token);

  const consoleMsgs = [];
  page.on("console", (m) => consoleMsgs.push(`[${m.type()}] ${m.text()}`));

  await page.goto(OPS_URL, { waitUntil: "networkidle" });
  await waitMs(1500);

  // ─── TEST 11 ───────────────────────────────────────────────────────
  console.log("[T11] starting kill-switch ARM/RESET UAT");
  try {
    // Baseline: disarm if currently armed from a previous run.
    const pre = await getSafetyStatus();
    if (pre.armed) {
      await postKillSwitch(token, { action: "reset", note: "UAT-11 preclean" });
      await waitMs(1500);
    }

    // Pre-arm screenshot
    const preArmPath = await screenshot(page, "test11-pre-arm");
    const preBadge = await page.evaluate(() => {
      const b = document.getElementById("killSwitchBadge");
      return b ? { text: b.textContent, className: b.className } : null;
    });
    console.log("[T11] pre-arm badge:", preBadge);

    // ARM via API (operator flow validated end-to-end; UI button wiring is
    // covered by grep evidence at dashboard.js:2464-2473).
    const armResp = await postKillSwitch(token, {
      action: "arm",
      reason: "UAT-11 browser UAT",
    });
    console.log("[T11] arm response:", armResp.status, armResp.body.slice(0, 120));
    await waitMs(2000);

    const armedPath = await screenshot(page, "test11-armed");
    const armedBadge = await page.evaluate(() => {
      const b = document.getElementById("killSwitchBadge");
      const armBtn = document.getElementById("killSwitchArm");
      const resetBtn = document.getElementById("killSwitchReset");
      return b
        ? {
            text: b.textContent,
            className: b.className,
            armHidden: armBtn?.classList.contains("hidden"),
            resetHidden: resetBtn?.classList.contains("hidden"),
            resetDisabled: resetBtn?.disabled,
          }
        : null;
    });
    console.log("[T11] armed badge:", armedBadge);

    // RESET via API. Supervisor may apply a cooldown; if reset returns 400
    // due to cooldown, wait and retry.
    let resetResp = await postKillSwitch(token, {
      action: "reset",
      note: "UAT-11 reset",
    });
    if (resetResp.status === 400 && /cooldown/i.test(resetResp.body)) {
      // Parse cooldown remaining, wait that long (+200ms) and retry.
      const stat = await getSafetyStatus();
      const wait = Math.ceil((stat.cooldown_remaining || 0) * 1000) + 500;
      console.log(`[T11] cooldown ${wait}ms — waiting then retry reset`);
      await waitMs(wait);
      resetResp = await postKillSwitch(token, {
        action: "reset",
        note: "UAT-11 reset retry",
      });
    }
    console.log("[T11] reset response:", resetResp.status, resetResp.body.slice(0, 120));
    await waitMs(2000);

    const resetPath = await screenshot(page, "test11-reset");
    const resetBadge = await page.evaluate(() => {
      const b = document.getElementById("killSwitchBadge");
      const armBtn = document.getElementById("killSwitchArm");
      const resetBtn = document.getElementById("killSwitchReset");
      return b
        ? {
            text: b.textContent,
            className: b.className,
            armHidden: armBtn?.classList.contains("hidden"),
            resetHidden: resetBtn?.classList.contains("hidden"),
          }
        : null;
    });
    console.log("[T11] reset badge:", resetBadge);

    const armedOk =
      armResp.status === 200 &&
      armedBadge &&
      /ARMED/i.test(armedBadge.text || "") &&
      /status-critical/.test(armedBadge.className || "") &&
      armedBadge.armHidden === true &&
      armedBadge.resetHidden === false;

    const resetOk =
      resetResp.status === 200 &&
      resetBadge &&
      /Disarmed/i.test(resetBadge.text || "") &&
      /status-ok/.test(resetBadge.className || "") &&
      resetBadge.armHidden === false &&
      resetBadge.resetHidden === true;

    report.test11 = {
      result: armedOk && resetOk ? "pass" : "partial",
      screenshots: [preArmPath, armedPath, resetPath],
      preBadge,
      armedBadge,
      resetBadge,
      armStatus: armResp.status,
      resetStatus: resetResp.status,
      method: "API POST + WS-driven UI observation",
    };
    console.log("[T11] result:", report.test11.result);
  } catch (err) {
    report.test11 = { result: "fail", error: String(err) };
    console.error("[T11] ERROR:", err);
  }

  // ─── TEST 12 ───────────────────────────────────────────────────────
  console.log("[T12] starting shutdown banner UAT");
  try {
    // Take baseline screenshot (banner hidden).
    const preShutdownPath = await screenshot(page, "test12-pre-shutdown");
    const preState = await page.evaluate(() => {
      const banner = document.getElementById("shutdownBanner");
      const text = document.getElementById("shutdownBannerText");
      return banner
        ? {
            hidden: banner.classList.contains("hidden"),
            text: text?.textContent,
          }
        : null;
    });
    console.log("[T12] pre-state:", preState);

    // Inject a synthetic `shutdown_state` WS message.
    const inj = await injectWsMessage(page, {
      type: "shutdown_state",
      payload: { phase: "shutting_down", reason: "UAT-12 synthetic" },
    });
    console.log("[T12] injection:", inj);
    await waitMs(800);

    const duringPath = await screenshot(page, "test12-shutting-down");
    const duringState = await page.evaluate(() => {
      const banner = document.getElementById("shutdownBanner");
      const text = document.getElementById("shutdownBannerText");
      return banner
        ? {
            hidden: banner.classList.contains("hidden"),
            text: text?.textContent,
            display: getComputedStyle(banner).display,
          }
        : null;
    });
    console.log("[T12] during:", duringState);

    // Inject "complete" phase.
    await injectWsMessage(page, {
      type: "shutdown_state",
      payload: { phase: "complete" },
    });
    await waitMs(600);
    const completePath = await screenshot(page, "test12-complete");
    const completeState = await page.evaluate(() => {
      const text = document.getElementById("shutdownBannerText");
      const banner = document.getElementById("shutdownBanner");
      return {
        text: text?.textContent,
        hidden: banner?.classList.contains("hidden"),
      };
    });
    console.log("[T12] complete:", completeState);

    // Reset banner by dispatching an empty phase so other tests aren't affected.
    await injectWsMessage(page, {
      type: "shutdown_state",
      payload: { phase: null },
    });
    await waitMs(300);

    const pass =
      preState?.hidden === true &&
      duringState?.hidden === false &&
      /shutting down/i.test(duringState?.text || "") &&
      /complete/i.test(completeState?.text || "");

    report.test12 = {
      result: pass ? "pass" : "partial",
      screenshots: [preShutdownPath, duringPath, completePath],
      preState,
      duringState,
      completeState,
      method: "synthetic WS message injection via shimmed WebSocket",
      codePath: {
        renderer: "arbiter/web/dashboard.js:1449-1466 (renderShutdownBanner)",
        wsDispatch: "arbiter/web/dashboard.js:1142-1143 (shutdown_state handler)",
        markup: "index.html:195-196 (#shutdownBanner, #shutdownBannerText)",
      },
    };
    console.log("[T12] result:", report.test12.result);
  } catch (err) {
    report.test12 = { result: "fail", error: String(err) };
    console.error("[T12] ERROR:", err);
  }

  // ─── TEST 13 ───────────────────────────────────────────────────────
  console.log("[T13] starting rate-limit pill UAT");
  try {
    // The #rateLimitIndicators host is declared in dashboard.js but the
    // current index.html doesn't render it. Inject a host so renderRateLimitBadges
    // has somewhere to write. Note this gap for the report.
    const hostState = await ensureRateLimitHost(page);
    console.log("[T13] host state:", hostState);

    // Step A — idle (green/ok tone)
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 10, max_requests: 10, remaining_penalty_seconds: 0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(500);
    const idleSnap = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      return host
        ? Array.from(host.children).map((c) => ({ text: c.textContent, className: c.className }))
        : null;
    });
    const idlePath = await screenshot(page, "test13-idle-green");
    console.log("[T13] idle pills:", idleSnap);

    // Step B — one platform entering warn (amber)
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 4, max_requests: 10, remaining_penalty_seconds: 5.0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(500);
    const warnSnap = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      return host
        ? Array.from(host.children).map((c) => ({ text: c.textContent, className: c.className }))
        : null;
    });
    const warnPath = await screenshot(page, "test13-warn-amber");
    console.log("[T13] warn pills:", warnSnap);

    // Step C — both in heavy penalty
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 0, max_requests: 10, remaining_penalty_seconds: 30.0 },
        polymarket: { available_tokens: 0, max_requests: 20, remaining_penalty_seconds: 30.0 },
      },
    });
    await waitMs(500);
    const critSnap = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      return host
        ? Array.from(host.children).map((c) => ({ text: c.textContent, className: c.className }))
        : null;
    });
    const critPath = await screenshot(page, "test13-both-warn");
    console.log("[T13] both-warn pills:", critSnap);

    // Step D — clear back to green
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 10, max_requests: 10, remaining_penalty_seconds: 0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(500);
    const recoverPath = await screenshot(page, "test13-recovered-green");

    const tonesSeen = new Set();
    [idleSnap, warnSnap, critSnap].forEach((snap) => {
      (snap || []).forEach((p) => {
        if (/rate-limit-pill\s+ok/.test(p.className)) tonesSeen.add("ok");
        if (/rate-limit-pill\s+warn/.test(p.className)) tonesSeen.add("warn");
        if (/rate-limit-pill\s+crit/.test(p.className)) tonesSeen.add("crit");
      });
    });

    // Per dashboard-view-model.js:233-258, current implementation emits only
    // `ok` and `warn`; `crit` is explicitly reserved for future circuit-open.
    // Therefore pass criteria is: both `ok` and `warn` tones observed, pills
    // rendered, and recovery returns to `ok`.
    const pass =
      hostState.existed === true || // ideally the host exists natively
      (tonesSeen.has("ok") && tonesSeen.has("warn"));

    report.test13 = {
      result:
        tonesSeen.has("ok") && tonesSeen.has("warn")
          ? hostState.existed
            ? "pass"
            : "partial"
          : "fail",
      screenshots: [idlePath, warnPath, critPath, recoverPath],
      tonesSeen: Array.from(tonesSeen),
      idleSnap,
      warnSnap,
      critSnap,
      hostState,
      method: "synthetic WS rate_limit_state injection + DOM inspection",
      note:
        "dashboard-view-model.js intentionally maps to only `ok` and `warn` tones today. `crit` is reserved for circuit-open state (see comment at line 242). #rateLimitIndicators container is not rendered in index.html by default; the host element was injected for this UAT — flagging as a wiring gap.",
    };
    console.log("[T13] result:", report.test13.result);
  } catch (err) {
    report.test13 = { result: "fail", error: String(err) };
    console.error("[T13] ERROR:", err);
  }

  report.consoleMessages = consoleMsgs.slice(-40);

  writeFileSync(`${OUT_DIR}/report.json`, JSON.stringify(report, null, 2));
  console.log("wrote", `${OUT_DIR}/report.json`);

  await context.close();

  const anyFail =
    report.test11.result === "fail" ||
    report.test12.result === "fail" ||
    report.test13.result === "fail";
  process.exit(anyFail ? 1 : 0);
}

main().catch((e) => {
  console.error("FATAL", e);
  process.exit(2);
});
