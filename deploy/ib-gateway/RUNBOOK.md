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
