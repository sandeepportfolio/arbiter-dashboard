#!/usr/bin/env node
/**
 * uat_post_04_09.mjs — Re-verify UATs 11, 12, 13 after Plan 04-09 lands.
 *
 * Differences from uat_11_13.mjs:
 *   - Test 13 does NOT inject a host element. It asserts that the native
 *     #rateLimitIndicators element exists on /ops after G-5 Part A landed.
 *   - Test 13 exercises the crit tone by sending a `system` WS bootstrap with
 *     collectors[platform].circuit.state = "open" (the production payload
 *     shape per dashboard.js:1112 `state.system = message.payload`) and then
 *     injecting a matching rate_limit_state payload. If crit tone does NOT
 *     emit, the G-5 Part B wiring failed end-to-end (even though vitest
 *     passes with a non-production state shape).
 *   - Test 13 also runs the `{collectors: ...}` top-level shape to confirm
 *     whether the code path is hitting the right reader.
 */

import { writeFileSync, existsSync, mkdirSync } from "node:fs";
import { chromium } from "playwright";

const BASE = "http://127.0.0.1:8080";
const OPS_URL = `${BASE}/ops`;
const OUT_DIR = "C:/Users/sande/Documents/arbiter-dashboard/output/uat-post-04-09";
const PROFILE_DIR = "C:/Users/sande/Documents/arbiter-dashboard/.playwright-post-04-09";
const UI_EMAIL = "sparx.sandeep@gmail.com";
const UI_PASSWORD = "saibaba";

if (!existsSync(OUT_DIR)) mkdirSync(OUT_DIR, { recursive: true });

const report = { test11: {}, test12: {}, test13: {} };

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

async function installWsHook(page) {
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

async function injectWsMessage(page, payload) {
  return await page.evaluate((msg) => {
    const sock = window.__uatLastSocket;
    if (!sock) return { ok: false, reason: "no socket captured" };
    sock.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(msg) }));
    return { ok: true };
  }, payload);
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

async function main() {
  const token = await loginViaApi();
  console.log("[setup] got session token");

  const context = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: true,
    viewport: { width: 1440, height: 900 },
  });
  const page = context.pages()[0] || (await context.newPage());

  await installWsHook(page);
  await authenticateContext(context, token);

  const consoleMsgs = [];
  page.on("console", (m) => consoleMsgs.push(`[${m.type()}] ${m.text()}`));

  await page.goto(OPS_URL, { waitUntil: "networkidle" });
  await waitMs(1500);

  // ─── TEST 11 — Kill-switch ARM/RESET (SAFE-01) ─────────────────────
  console.log("[T11] starting kill-switch ARM/RESET");
  try {
    const pre = await getSafetyStatus();
    if (pre.armed) {
      await postKillSwitch(token, { action: "reset", note: "UAT-11 preclean" });
      await waitMs(1500);
    }

    const preArmPath = await screenshot(page, "test11-pre-arm");
    const preBadge = await page.evaluate(() => {
      const b = document.getElementById("killSwitchBadge");
      return b ? { text: b.textContent.trim(), className: b.className } : null;
    });

    const armResp = await postKillSwitch(token, { action: "arm", reason: "UAT-11 post-04-09" });
    await waitMs(2000);
    const armedPath = await screenshot(page, "test11-armed");
    const armedBadge = await page.evaluate(() => {
      const b = document.getElementById("killSwitchBadge");
      const armBtn = document.getElementById("killSwitchArm");
      const resetBtn = document.getElementById("killSwitchReset");
      return b ? {
        text: b.textContent.trim(),
        className: b.className,
        armHidden: armBtn?.classList.contains("hidden"),
        resetHidden: resetBtn?.classList.contains("hidden"),
      } : null;
    });

    let resetResp = await postKillSwitch(token, { action: "reset", note: "UAT-11 post-04-09 reset" });
    if (resetResp.status === 400 && /cooldown/i.test(resetResp.body)) {
      const stat = await getSafetyStatus();
      const wait = Math.ceil((stat.cooldown_remaining || 0) * 1000) + 500;
      await waitMs(wait);
      resetResp = await postKillSwitch(token, { action: "reset", note: "UAT-11 retry" });
    }
    await waitMs(2000);
    const resetPath = await screenshot(page, "test11-reset");
    const resetBadge = await page.evaluate(() => {
      const b = document.getElementById("killSwitchBadge");
      const armBtn = document.getElementById("killSwitchArm");
      const resetBtn = document.getElementById("killSwitchReset");
      return b ? {
        text: b.textContent.trim(),
        className: b.className,
        armHidden: armBtn?.classList.contains("hidden"),
        resetHidden: resetBtn?.classList.contains("hidden"),
      } : null;
    });

    const armedOk =
      armResp.status === 200 &&
      /ARMED/i.test(armedBadge?.text || "") &&
      /status-critical/.test(armedBadge?.className || "") &&
      armedBadge?.armHidden === true &&
      armedBadge?.resetHidden === false;
    const resetOk =
      resetResp.status === 200 &&
      /Disarmed/i.test(resetBadge?.text || "") &&
      /status-ok/.test(resetBadge?.className || "") &&
      resetBadge?.armHidden === false &&
      resetBadge?.resetHidden === true;

    report.test11 = {
      result: armedOk && resetOk ? "pass" : "partial",
      screenshots: [preArmPath, armedPath, resetPath],
      preBadge, armedBadge, resetBadge,
      armStatus: armResp.status, resetStatus: resetResp.status,
    };
    console.log("[T11] result:", report.test11.result);
  } catch (err) {
    report.test11 = { result: "fail", error: String(err) };
    console.error("[T11] ERROR:", err);
  }

  // ─── TEST 12 — Shutdown banner (SAFE-05) ────────────────────────────
  console.log("[T12] starting shutdown banner");
  try {
    const preShutdownPath = await screenshot(page, "test12-pre-shutdown");
    const preState = await page.evaluate(() => {
      const banner = document.getElementById("shutdownBanner");
      const text = document.getElementById("shutdownBannerText");
      return banner ? { hidden: banner.classList.contains("hidden"), text: text?.textContent?.trim() } : null;
    });

    await injectWsMessage(page, {
      type: "shutdown_state",
      payload: { phase: "shutting_down", reason: "UAT-12 post-04-09 synthetic" },
    });
    await waitMs(800);
    const duringPath = await screenshot(page, "test12-shutting-down");
    const duringState = await page.evaluate(() => {
      const banner = document.getElementById("shutdownBanner");
      const text = document.getElementById("shutdownBannerText");
      return banner ? {
        hidden: banner.classList.contains("hidden"),
        text: text?.textContent?.trim(),
        display: getComputedStyle(banner).display,
      } : null;
    });

    await injectWsMessage(page, {
      type: "shutdown_state",
      payload: { phase: "complete" },
    });
    await waitMs(600);
    const completePath = await screenshot(page, "test12-complete");
    const completeState = await page.evaluate(() => {
      const text = document.getElementById("shutdownBannerText");
      const banner = document.getElementById("shutdownBanner");
      return { text: text?.textContent?.trim(), hidden: banner?.classList.contains("hidden") };
    });

    // Reset banner so subsequent tests aren't affected.
    await injectWsMessage(page, { type: "shutdown_state", payload: { phase: null } });
    await waitMs(300);

    const pass =
      preState?.hidden === true &&
      duringState?.hidden === false &&
      /shutting down/i.test(duringState?.text || "") &&
      /complete/i.test(completeState?.text || "");

    report.test12 = {
      result: pass ? "pass" : "partial",
      screenshots: [preShutdownPath, duringPath, completePath],
      preState, duringState, completeState,
    };
    console.log("[T12] result:", report.test12.result);
  } catch (err) {
    report.test12 = { result: "fail", error: String(err) };
    console.error("[T12] ERROR:", err);
  }

  // ─── TEST 13 — Rate-limit pills ok→warn→crit (SAFE-04, G-5) ─────────
  console.log("[T13] starting rate-limit pill transitions");
  try {
    // Reload to get a clean page state (remove synthetic shutdown residue).
    await page.reload({ waitUntil: "networkidle" });
    await waitMs(1500);

    // Confirm the #rateLimitIndicators host is NATIVE (G-5 Part A).
    const hostState = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      if (!host) return { existed: false };
      return {
        existed: true,
        className: host.className,
        parentId: host.parentElement?.id,
        parentTag: host.parentElement?.tagName,
      };
    });
    console.log("[T13] host:", hostState);

    // Step A — idle (both ok)
    await injectWsMessage(page, {
      type: "system",
      payload: {
        collectors: {
          kalshi: { circuit: { state: "closed" } },
          polymarket: { circuit: { state: "closed" } },
        },
      },
    });
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 10, max_requests: 10, remaining_penalty_seconds: 0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(400);
    const idleSnap = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      return host ? Array.from(host.children).map((c) => ({
        text: c.textContent.trim(), className: c.className,
      })) : null;
    });
    const idlePath = await screenshot(page, "test13-idle");

    // Step B — kalshi warn (remaining_penalty_seconds > 0).
    // The server's periodic _rate_limit_task broadcasts the REAL limiter state
    // every few seconds, which will clobber our synthetic injection. Send the
    // message and snapshot immediately (next microtask) to read before the
    // next server broadcast wins.
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 4, max_requests: 10, remaining_penalty_seconds: 5.0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(50);
    const warnSnap = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      return host ? Array.from(host.children).map((c) => ({
        text: c.textContent.trim(), className: c.className,
      })) : null;
    });
    const warnPath = await screenshot(page, "test13-warn");

    // Step C — circuit OPEN → crit. This is the G-5 Part B validator.
    // Send a system message with collectors[kalshi].circuit.state === "open"
    // (matches the production WS bootstrap shape).
    await injectWsMessage(page, {
      type: "system",
      payload: {
        collectors: {
          kalshi: { circuit: { state: "open" } },
          polymarket: { circuit: { state: "closed" } },
        },
      },
    });
    // rate_limit_state must also be resent because render reads from both.
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 0, max_requests: 10, remaining_penalty_seconds: 30.0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(400);
    const critSnap = await page.evaluate(() => {
      const host = document.getElementById("rateLimitIndicators");
      // Also expose the state shape the renderer saw for debugging.
      const sysShape = {
        systemCollectors: Object.keys(window.state?.system?.collectors || {}),
        topLevelCollectors: Object.keys(window.state?.collectors || {}),
        kalshiCircuit: window.state?.system?.collectors?.kalshi?.circuit?.state,
      };
      return {
        pills: host ? Array.from(host.children).map((c) => ({
          text: c.textContent.trim(), className: c.className,
        })) : null,
        debug: sysShape,
      };
    });
    const critPath = await screenshot(page, "test13-crit-expected");

    // Step D — recover (circuit closed, tokens full)
    await injectWsMessage(page, {
      type: "system",
      payload: {
        collectors: {
          kalshi: { circuit: { state: "closed" } },
          polymarket: { circuit: { state: "closed" } },
        },
      },
    });
    await injectWsMessage(page, {
      type: "rate_limit_state",
      payload: {
        kalshi: { available_tokens: 10, max_requests: 10, remaining_penalty_seconds: 0 },
        polymarket: { available_tokens: 20, max_requests: 20, remaining_penalty_seconds: 0 },
      },
    });
    await waitMs(400);
    const recoverPath = await screenshot(page, "test13-recover");

    const tonesSeen = new Set();
    [idleSnap, warnSnap, critSnap.pills].forEach((snap) => {
      (snap || []).forEach((p) => {
        if (/rate-limit-pill\s+ok/.test(p.className)) tonesSeen.add("ok");
        if (/rate-limit-pill\s+warn/.test(p.className)) tonesSeen.add("warn");
        if (/rate-limit-pill\s+crit/.test(p.className)) tonesSeen.add("crit");
      });
    });

    const kalshiCritEmitted = (critSnap.pills || []).some(
      (p) => /kalshi/i.test(p.text) && /rate-limit-pill\s+crit/.test(p.className),
    );

    report.test13 = {
      result: (hostState.existed && tonesSeen.has("ok") && tonesSeen.has("warn") && kalshiCritEmitted) ? "pass" : "partial",
      screenshots: [idlePath, warnPath, critPath, recoverPath],
      hostState,
      tonesSeen: Array.from(tonesSeen),
      kalshiCritEmitted,
      idleSnap, warnSnap, critSnap,
    };
    console.log("[T13] result:", report.test13.result);
    console.log("[T13] tonesSeen:", Array.from(tonesSeen));
    console.log("[T13] kalshi crit emitted:", kalshiCritEmitted);
  } catch (err) {
    report.test13 = { result: "fail", error: String(err) };
    console.error("[T13] ERROR:", err);
  }

  report.consoleMessages = consoleMsgs.slice(-40);
  writeFileSync(`${OUT_DIR}/report.json`, JSON.stringify(report, null, 2));
  console.log("wrote", `${OUT_DIR}/report.json`);

  await context.close();

  const anyFail = report.test11.result === "fail" || report.test12.result === "fail" || report.test13.result === "fail";
  process.exit(anyFail ? 1 : 0);
}

main().catch((e) => { console.error("FATAL", e); process.exit(2); });
