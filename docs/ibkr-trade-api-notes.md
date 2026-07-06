# IBKR Client Portal Web API — trade & auth notes for Arbiter

Researched 2026-07-06 against official IBKR docs (Campus CPAPI v1 reference, Event Contracts
section, ForecastTrader). Gateway: `~/ibkr-gateway/` (Java CP gateway), serves
`https://localhost:5000/v1/api` (self-signed TLS; from Docker: `https://host.docker.internal:5000/v1/api`).
Live account `U25953084`, `allowEventContract: true`.

## Auth lifecycle (two-tier session model)

- **Outer SSO/Portal session** — created by the interactive browser login at
  `https://localhost:5000`. Required for all requests. Read-only.
- **Inner brokerage session** ("iserver") — required for `/iserver/*` (trading, market data).

| Endpoint | Notes |
|---|---|
| `POST /iserver/auth/status` (empty body) | `authenticated`, `competing`, `connected`, `fail`. `connected:true, authenticated:false` = **soft expiry**. All POSTs need a body (`-d ''`) or Akamai returns HTML 411. |
| `POST /tickle` | Keep-alive for both tiers. Call **every 60 s** (brokerage session dies after ~5 min idle). Rate limit 1/s. Returns `ssoExpires` (ms) + embedded `iserver.authStatus`. |
| `GET /sso/validate` | Outer-session check. Rate limit **1/min**. |
| `POST /iserver/auth/ssodh/init` body `{"publish":true,"compete":true}` | **The soft-expiry recovery path** (officially recommended; `/iserver/reauthenticate` is deprecated and often no-ops). `compete:true` seizes a competing/stale brokerage session — this fixed the 2026-07-06 `"sso dh already set"` / `Invalid_username_or_password Error code =1` lockup without a human re-login. |
| `POST /logout` | Ends the gateway session. |

**Soft vs hard expiry:** soft (brokerage dropped, SSO alive) → recover with `ssodh/init`.
Hard (SSO itself gone: `sso/validate` fails / `connected:false`) → **only** a browser
login + 2FA fixes it. There is no official headless login; IBKR explicitly disclaims
automation of the SSO login (IBeam etc.).

**Daily reset:** sessions hard-reset at regional midnight (America/New_York for US) —
plan for **one browser+2FA login per day**; competing logins (TWS/mobile/web with the same
username) kill the API brokerage session (`competing:true`) — reclaim with
`ssodh/init compete:true` (which boots the other session). Long-term zero-login fix is the
dormant `ib-gateway` (IBC) docker service + dedicated bot username per
`deploy/ib-gateway/RUNBOOK.md` (needs human account admin: bot username + ForecastEx
permission + IB Key).

## Order placement

`POST /iserver/account/{accountId}/orders` body `{"orders":[{...}]}` — one unrelated order
at a time; never place another order until the previous is fully acknowledged.

Key fields: `conid` (int; forces SMART routing) or `conidex` (`"conid@EXCHANGE"` for direct
routing), `side` `BUY|SELL`, `orderType` `LMT|MKT|...`, `price` (LMT), `quantity` (whole
contracts), `tif` `DAY|GTC|IOC|...`, `cOID` (customer order id, unique for 24 h, ≤64 chars,
round-trips as `order_ref`), `listingExchange`.

**Three 200-OK response shapes — branch on shape, not status code:**
1. Success: `[{"order_id":"...","order_status":"Submitted",...}]`
2. Confirmation required: `[{"id":"<replyId>","message":[...],"messageIds":["o163"]}]` →
   `POST /iserver/reply/{replyId}` `{"confirmed":true}`, **loop** (more replies may follow).
   Must reply immediately; any other request cancels the pending order (late reply → 503).
3. Reject: `{"error":"..."}` (still HTTP 200).

Suppression: `POST /iserver/questions/suppress` `{"messageIds":["o163","o354","o382","o383","o403","o451","o10151","o10153","o10336"]}`
(per-session; reset via `/suppress/reset`).

Status: `GET /iserver/account/orders` (1 req/5 s; first call may return an empty snapshot —
re-poll, `force=true` busts cache). Single: `GET /iserver/account/order/status/{orderId}`.
Cancel: `DELETE /iserver/account/{accountId}/order/{orderId}` — **never let orderId default
to -1** (cancels ALL). Prereq: `GET /iserver/accounts` must be called once per brokerage
session before order/cancel/modify/snapshot traffic.

## ForecastEx event contracts

- Modeled as **OPT** under an artificial underlying conid. Each question has **two conids**:
  YES (call) and NO (put) — the repo already stores both per mapping
  (`/api/forecastex/diagnostics`).
- Discovery: `/forecast/category/tree`, `/forecast/contract/market?underlyingConid=&exchange=FORECASTX`,
  `/forecast/contract/details?conid=` (returns `conid_yes` + `conid_no`),
  `/forecast/contract/rules?conid=` (price_increment $0.01, payout $1.00).
- Prices: **$0.01–$0.99, $0.01 increments**, `price` field is literal dollars (e.g. `0.57`).
  Whole-contract quantities, min 1.
- **Buy-only venue**: SELL orders are rejected. Exit/reduce = **BUY the opposing conid**;
  IB nets automatically, emitting transient `side:"N"` (Netting) execution reports and a
  momentary opposite-side position. Fill reconciliation must tolerate these
  (`is_event_trading` flags them).
- Exchange string inconsistency in official docs: `FORECASTX` in `/forecast/*` data vs
  `FORECASTEX` in the order example — verify `validExchanges` via `/iserver/secdef/info`
  before direct routing.

## Pacing / gotchas

- Global 10 req/s; violations → 15-min IP penalty box (429). `/tickle` 1/s,
  `/sso/validate` 1/min, `/iserver/account/orders` 1 per 5 s.
- macOS AirPlay Receiver also wants port 5000 — if the gateway fails to bind, that's why.
- Paper trading needs the separate paper username at gateway login (no toggle);
  ForecastEx does **not** exist on paper.
- Repo integration: `arbiter/collectors/forecastex.py` (`ForecastExClient`) is the live
  REST path (`IBKR_GATEWAY_URL`, `IBKR_VERIFY_SSL=false`); it lazily re-inits the brokerage
  bridge on `400 "no bridge"` via `ssodh/init compete:true`. Circuit `forecastex-rest`:
  10 consecutive failures → OPEN, auto half-open after 120 s, 2 successes → CLOSED — no
  manual reset exists; it self-heals once the gateway authenticates.
