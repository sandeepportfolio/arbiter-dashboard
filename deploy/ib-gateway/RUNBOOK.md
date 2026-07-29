# ForecastEx (IBKR) — go-live runbook: never log in by hand again

This is the one-time setup to get **ForecastEx trades executing automatically**
across the bot, and to stop the ~30 manual logins. There are two paths:

- **Path A — IB Gateway + IBC (do this first; live this week).** The bot's
  credentials are stored once; a headless IB Gateway logs itself in and
  restarts nightly. Robust fills (synchronous broker order-id). Steady state:
  **one IBKR-Mobile tap per week** (or zero, with the optional opt-out in §A6).
- **Path B — Web API OAuth 1.0a (the "API keys", zero-touch endgame).** No
  gateway, no login, no weekly tap — pure machine-to-machine keys. Takes IBKR
  ~1-2 weeks to activate, so run it in parallel and cut over later.

> Root cause of the current `No trading permissions` / 0 fills: the bot's IBKR
> **username** does not hold the ForecastEx permission (permissions bind to the
> *username*, not the account), and the Client Portal gateway's SSO keeps
> expiring. Both paths below fix this.

---

## Path A — IB Gateway + IBC (fastest to live)

### A1. Create a dedicated **bot** IBKR username
Client Portal → **Settings → Users & Access Rights → add a user** for the same
account (`U25953084`). Give it trading rights. **Why:** IBKR allows only one
brokerage session per username — if you log into TWS/Client Portal/IBKR Mobile
with the *same* username the bot uses, you evict the bot mid-trade. The bot gets
its own username; you keep yours.

### A2. Enable the ForecastEx trading permission **on the bot username**
Client Portal (logged in as / configuring the **bot** username) →
**Settings → Account Configuration → Trading Permissions** → enable
**“Event Contracts – ForecastEx”** and accept the ForecastEx Disclosure of Risk
+ CFTC Event Contract Acknowledgement (17 CFR 33.7).
Docs: https://www.ibkrguides.com/clientportal/tradingpermissions.htm
Eligibility (must be an eligible US/IBKR-LLC client; some contracts geo-gated):
https://www.ibkrguides.com/orgportal/trade/eventtrader.htm

### A3. Set up the 2FA device for the bot username
Install **IBKR Mobile** and activate **IB Key** for the bot username. You'll tap
it once at first login and ~once a week after the Sunday 01:00 ET token reset.

### A4. Put the bot credentials in `.env.production`
The prod compose already defines the `ib-gateway` service. Set:
```
IBKR_USERNAME=<dedicated bot username from A1>
IBKR_PASSWORD=<its password>
IBKR_TRADING_MODE=live          # ForecastEx is NOT on paper accounts
IBKR_TWS_TIME_ZONE=America/New_York
```
`.env.production` is `chmod 600` and gitignored — never commit it.

### A5. Bring up the gateway (the bot is NOT trading yet)
```
docker compose -f docker-compose.prod.yml --env-file .env.production up -d ib-gateway
docker logs -f arbiter-ib-gateway-prod      # watch for "Login has completed"
```
On first start, approve the IBKR-Mobile push. The arbiter does **not**
`depends_on` the gateway, so this can't disrupt the running API. Nothing trades:
`IBKR_USE_TWS` is still false.

### A6. (Optional) zero weekly taps
To remove even the weekly tap on this path: Client Portal →
**Settings → User Settings → Secure Login System → “Secure Login required for
trading” → “only when logging into Account Management.”** This de-2FAs the
*trading* login (Account Management keeps 2FA). **Trade-off:** it forfeits
IBKR's account-compromise loss guarantees, so only do this if (a) the gateway
port stays loopback/internal-only (it is — see compose) and (b) you accept that.
Otherwise keep the weekly tap. If you want true zero-touch *with* the
guarantees, use **Path B** instead.
Docs: https://www.interactivebrokers.com/en/software/am/am/manageaccount/sls_opt_out.htm

### A7. Verify permission safely, then go live (I drive this with you)
Once the gateway shows logged-in, **I** run a `whatIf` preview order (no
execution) to confirm the permission + margin, then a single **$1** live buy to
confirm the broker order-id round-trip and fill. Only after that do we flip the
gated switches in `.env.production`:
```
IBKR_USE_TWS=true
IBKR_TWS_ALLOW_LIVE=true     # explicit live opt-in (new safety gate)
IBKR_PAPER_TRADING=false
```
and restart `arbiter-api-prod`. (Until both `IBKR_USE_TWS` and
`IBKR_TWS_ALLOW_LIVE` are set, the client refuses to route live orders.)

---

## Path B — Web API OAuth 1.0a (the "API keys", zero-touch)

### B1. Generate the key material (local, safe — touches nothing online)
```
python scripts/ibkr_oauth_setup.py
```
Writes RSA signing + encryption keypairs and DH params to the **gitignored**
`deploy/ib-gateway/oauth/` and prints the three public artifacts to register.

### B2. Register (one interactive login — the last one)
Log in to the IBKR **self-service OAuth** portal (US: Settings → User Settings →
API / “Third Party Self-Service OAuth”) and upload:
`public_signature.pem`, `public_encryption.pem`, `dhparam.pem`.
IBKR processes the registration on a weekend; allow **~1-2 weeks**. You'll
receive a ~9-character **consumer key**.

### B3. Wire it up
In `.env.production`:
```
IBKR_OAUTH_CONSUMER_KEY=<9-char key>
IBKR_OAUTH_SIGNATURE_KEY=deploy/ib-gateway/oauth/private_signature.pem
IBKR_OAUTH_ENCRYPTION_KEY=deploy/ib-gateway/oauth/private_encryption.pem
IBKR_OAUTH_DH_PARAM=deploy/ib-gateway/oauth/dhparam.pem
```
The bot then derives a ~24h live session token machine-to-machine (auto
`ssodh/init` + `/tickle` keep-alive) — no gateway, no login, no 2FA tap.

> **Note:** OAuth rides the same Web API REST order endpoints as the old Client
> Portal path, so it does **not** by itself fix the broker order-id / fill
> robustness that the TWS socket (Path A) gives. Best combo: **Path A as the
> trading transport** + Path B only if you must eliminate the weekly tap. We can
> revisit once B is active.

---

## What you do vs. what I do

| Step | You | Me |
|------|-----|----|
| Dedicated bot username (A1) | ✅ | |
| Enable ForecastEx permission on it (A2) | ✅ | |
| 2FA device (A3) | ✅ | |
| Credentials into `.env.production` (A4) | ✅ (or paste them and I'll place them) | |
| Bring up gateway (A5) | | ✅ |
| `whatIf` preview + $1 verify (A7) | approve the one push | ✅ |
| Flip the gated live switches (A7) | ✅ sign-off | ✅ apply |
| OAuth keygen + registration (B1–B2) | ✅ the one login | ✅ generate keys + wire |

Tell me when A1–A4 are done (or paste the bot username/password and I'll put them
in `.env.production` for you) and I'll bring the gateway up and run the $1
verification.

---

## Path B — STATUS 2026-07-13 (build progress)

**Done (autonomous):**
- ✅ Key material generated → `deploy/ib-gateway/oauth/` (gitignored): private/public
  signature + encryption keys + `dhparam.pem`.
- ✅ OAuth 1.0a module built + unit-tested: `arbiter/auth/ibkr_oauth.py`
  (RSA-SHA256 signing, DH Live-Session-Token handshake, HMAC-SHA256 request
  signing). 6 tests incl. a full LST handshake simulated end-to-end.
- ✅ Config wired: `ForecastExConfig.oauth_*` fields + `oauth_configured` gate.
- ✅ `.env.production` OAuth placeholders appended (key-file paths set; consumer
  key/token blank until IBKR issues them).
- ✅ Registration page opened in the operator's browser.

**Operator action (the one login):** log in at the OAuth self-service page, pick
a 9-char consumer key, upload `public_signature.pem`, `public_encryption.pem`,
`dhparam.pem`; Save Key → Generate Token → Enable OAuth Access. IBKR activates
in ~1-2 weeks and issues the consumer key + access token/secret.

**Update 2026-07-17 — client wiring DONE (was step 2 below).** `IbkrOAuth1a` is
wired into `ForecastExClient` (`oauth` field): api.ibkr.com base, per-attempt
`auth_header` with query params in the signature base, auto
`ensure_live_session_token()` (async DH handshake + signature validation),
signed `ssodh/init` bridge, LST invalidation on 401, verified TLS in OAuth
mode. Gated by `IBKR_OAUTH_ENABLED` (explicit switch) AND `oauth_configured`
(all material present) — attached in `build_forecastex_collector`
(`arbiter/main.py`). Tests: `arbiter/auth/test_ibkr_oauth.py` +
`arbiter/collectors/test_forecastex_oauth.py`.

**Remaining cutover (config-only, when the consumer key arrives):**
1. Set `IBKR_OAUTH_CONSUMER_KEY` / `IBKR_OAUTH_ACCESS_TOKEN` /
   `IBKR_OAUTH_ACCESS_TOKEN_SECRET` in `.env.production`.
2. Validate live: `set -a; source .env.production; set +a &&
   python3 scripts/validate_ibkr_oauth.py` — proves LST handshake +
   signature verification + a signed 200 read against the real api.ibkr.com.
3. Set `IBKR_OAUTH_ENABLED=true`, rebuild + redeploy. Gateway path stays as
   fallback (flip the flag back off to return to it).

Until then: the `/tickle` keepalive (already live) removes the frequent
soft-expiry logins; only IBKR's ~daily hard reset needs a browser re-login.

---

## Path B — STATUS 2026-07-21 (incident follow-up)

**Runtime today:** CP gateway transport, healthy (authenticated, keepalive ok).
The Jul 19-20 outage was ~23h of dead SSO after the keepalive restarted the
gateway at 23:10 PT and nobody performed the fresh browser login until 22:18 PT
next day. Until Path B activates, every gateway restart repeats this exposure.

**Fixes deployed (commit 574a75f):** an SSO outage can no longer permanently
quarantine healthy mappings (resolve_event_children now raises when zero month
probes complete; the resolver's forecastex_not_available mark now uses the
durable single-column write instead of upsert, which silently dropped it), the
conid-"0" junk rows are filtered everywhere, matcher inserts without a resolved
YES/NO child pair fail closed to review, and `oauth_configured` now requires
IBKR_OAUTH_ACCESS_TOKEN_SECRET so a partial cutover cannot take FX dark.

**★ OPERATOR ACTION (unchanged, still the only blocker):** the IBKR portal
(www.interactivebrokers.com) is NOT logged in on this machine, so the B2
registration state could not be verified autonomously — the last recorded step
is "registration page opened" on 2026-07-13, with NO record that the upload was
submitted. Log in (credentials + 2FA) and check Settings → User Settings → API
→ Third-Party Self-Service OAuth:
1. If no registration exists: upload `deploy/ib-gateway/oauth/public_signature.pem`,
   `public_encryption.pem`, `dhparam.pem` (all three verified valid and ready);
   pick a 9-char consumer key; Save Key → Generate Token → Enable OAuth Access.
   NEVER upload/regenerate the private keys; do not re-run the keygen with
   --force (it would invalidate the registration).
2. If pending: nothing to do — IBKR activates on a weekend (~1-2 weeks).
3. If active (consumer key + access token/secret issued): run
   `scripts/complete_oauth_cutover.sh <consumer_key> <access_token> <secret>` —
   it validates the live LST handshake + a signed 200 read BEFORE flipping
   IBKR_OAUTH_ENABLED, and auto-rolls back to the gateway transport on failure.

**Path A note:** `arbiter-ib-gateway-prod` remains stopped (SIGTERMed during
the Jul 20 deploy). It can never log in until A1-A4 are done — IBKR_USERNAME /
IBKR_PASSWORD are still empty in `.env.production`. Leave it stopped until then.

---

## Path B — STATUS 2026-07-29 ✅ CUTOVER COMPLETE

**ForecastEx now runs on OAuth 1.0a against api.ibkr.com — zero-login, permanent.**
`live_trading_ready: true`, FX collector 0 errors, no gateway session required.

The registration was NEVER the blocker — the three PEMs were uploaded and IBKR
had already issued the `ARBITERFX` consumer key + access token (present in
`.env.production` all along). Two code bugs blocked activation:

1. **Double percent-encoded OAuth signature** (`arbiter/auth/ibkr_oauth.py`):
   `_rsa_sha256_sign`/`_hmac_sha256_sign` returned an already-quoted signature
   and `_oauth_header` quoted every value again → IBKR error 23804
   "Error validating signature" on every LST handshake. Signatures now return
   raw base64; the header quotes exactly once (matches ibind + IBKR sample).
   Regression test: `test_lst_header_signature_is_percent_encoded_exactly_once`.
2. **cryptography 49 rejects the registered dhparam.pem** ("Invalid DH
   parameters") which cryptography 46 accepted — the container crash-looped
   the entire API at boot the moment `IBKR_OAUTH_ENABLED=true` (this is why
   the first cutover attempt auto-rolled back). `_read_dh_params` now decodes
   the PKCS#3 ASN.1 itself; the IBKR-registered file must never be regenerated.

Also fixed: `complete_oauth_cutover.sh` polled the authenticated
`/api/readiness` (401 since the 2026-07-25 auth lockdown) and would roll back
every good cutover; it now polls the public `/ready` probe.

**The local CP gateway (`arbiter-ib-gateway-prod`), keepalive supervisor and
daily browser re-login are now OPTIONAL.** Flip `IBKR_OAUTH_ENABLED` off to
return to the gateway transport. LST auto-refreshes ~every 22h in-process.
