# Pitfalls Research

**Domain:** Cross-platform prediction market arbitrage (Kalshi, Polymarket, PredictIt)
**Researched:** 2026-04-16
**Confidence:** HIGH (verified against official API docs, open-source issue trackers, and codebase inspection)

## Critical Pitfalls

### Pitfall 1: Kalshi API Price Format Is Broken in Current Codebase

**What goes wrong:**
The codebase submits Kalshi orders using integer cent fields (`yes_price`, `no_price` as integers 1-99). Kalshi **removed all legacy integer-cent price fields from API responses on March 12, 2026** and migrated to dollar-denominated string fields (`yes_price_dollars`, e.g., `"0.6500"`). The current order submission code at `engine.py:819-831` will fail with API errors on every single order.

**Why it happens:**
The code was written against the pre-March 2026 Kalshi API. The migration from `yes_price: 65` (cents integer) to `yes_price_dollars: "0.65"` (dollar string) was a breaking change that also introduced fractional trading (`fractional_trading_enabled` flag on market responses, `count_fp` for fractional contract counts).

**How to avoid:**
- Migrate all Kalshi order submission to use `yes_price_dollars` (dollar string with up to 4 decimal places) instead of `yes_price` (integer cents)
- Replace `count` with `count_fp` for fractional-enabled markets
- Check `fractional_trading_enabled` on each market response and handle both formats
- Update the collector's `_build_price_point` to prefer `*_dollars` fields (partially done -- the collector reads `yes_bid_dollars` etc. but the order submission ignores this)

**Warning signs:**
- Every Kalshi order returns HTTP 400 or 422 in production
- Demo environment may still accept legacy fields temporarily -- passing demo tests does NOT mean production works

**Phase to address:**
Phase 1 (API Integration Audit) -- this is a **hard blocker** for any live Kalshi trading.

**Confidence:** HIGH -- verified against [Kalshi API Changelog](https://docs.kalshi.com/changelog) and [Kalshi API docs](https://docs.kalshi.com/api-reference/orders/create-order).

---

### Pitfall 2: Polymarket Client Missing Signature Type and Funder Address

**What goes wrong:**
The Polymarket CLOB client initialization at `engine.py:896-904` creates a `ClobClient` without specifying `signature_type` or `funder` address. If the wallet is a proxy/browser wallet (which Polymarket commonly uses), orders will fail with 401 Unauthorized errors because the signing key differs from the address holding the funds.

**Why it happens:**
Polymarket has three signature types: `0` (standard EOA), `1` (email/Magic wallet), `2` (browser proxy). The py-clob-client defaults to type 0, which only works for direct private key wallets. Even with the correct type, the `funder` parameter is required when the proxy wallet address (displayed on Polymarket.com) differs from the signing key address.

**How to avoid:**
- Determine the correct `signature_type` for the wallet being used and pass it during `ClobClient` initialization
- Always set the `funder` parameter to the proxy wallet address if using types 1 or 2
- Test authentication by calling `client.get_api_keys()` before attempting any orders -- this validates the full auth chain
- Note: Even with valid L2 API credentials, order creation still requires the wallet to sign the order payload, so key management must be correct end-to-end

**Warning signs:**
- 401 Unauthorized on `post_order()` while other endpoints (e.g., `cancel_all()`) work fine
- `PolyApiException[status_code=401, error_message={'error': 'Unauthorized/Invalid api key'}]` on order calls specifically

**Phase to address:**
Phase 1 (API Integration Audit) -- **hard blocker** for Polymarket trading.

**Confidence:** HIGH -- verified against [py-clob-client issues #187](https://github.com/Polymarket/py-clob-client/issues/187), [#278](https://github.com/Polymarket/py-clob-client/issues/278), and [Polymarket Authentication docs](https://docs.polymarket.com/api-reference/authentication).

---

### Pitfall 3: Settlement Resolution Divergence Turns "Risk-Free" Arbitrage Into Total Loss

**What goes wrong:**
Cross-platform arbitrage assumes the same event resolves identically on both platforms. In practice, Kalshi and Polymarket use fundamentally different resolution criteria, different oracles, and different dispute processes. Holding opposite positions across platforms can result in **both legs losing** when platforms resolve the same event differently.

**Why it happens:**
- Kalshi uses internal resolution teams bound by CFTC rules (cannot profit from death, specific legal definitions)
- Polymarket uses the UMA Optimistic Oracle with token-holder voting (vulnerable to whale manipulation -- see March 2025 Ukraine mineral deal incident where a whale with 25% of UMA voting power manipulated a $7M resolution)
- Identical-sounding market titles mask different resolution criteria (e.g., Cardi B Super Bowl: Kalshi resolved NO via Rule 6.3(c), Polymarket resolved YES based on media consensus -- $57M+ in volume affected)
- The February 2026 Khamenei market: Polymarket resolved YES ($529M payout), Kalshi settled at last traded price due to CFTC death-profit restrictions

**How to avoid:**
- **Before mapping any market pair**, read the full resolution criteria on BOTH platforms, not just the title
- Store resolution criteria in the market mapping config for human review
- Set a minimum spread threshold of 15+ cents for cross-platform arbs to compensate for resolution risk (per industry guidance)
- Flag markets involving deaths, ambiguous performance criteria, or subjective judgments as "resolution_risk: high" and require manual approval
- Never rely on title matching alone -- `similarity_score()` text overlap says nothing about resolution semantics
- Consider limiting cross-platform arbs to markets with identical, objective resolution criteria (e.g., "Will BTC exceed $X by date Y?" using the same price source)

**Warning signs:**
- Market titles match but resolution criteria text differs in any way
- Markets involving subjective judgments ("Will X perform?", "Will X agree to Y?")
- Markets on events where regulatory treatment differs (death, gambling, sports)
- UMA dispute activity on the Polymarket side

**Phase to address:**
Phase 1 (Market Mapping Audit) and ongoing operational discipline. This is not a one-time fix -- every new market pair needs resolution criteria review.

**Confidence:** HIGH -- documented in [DeFi Rate settlement analysis](https://defirate.com/prediction-markets/how-contracts-settle/), [Finance Magnates Iran case](https://www.financemagnates.com/cryptocurrency/us-military-action-against-iran-exposes-split-between-polymarket-and-kalshi-models/), and [UMA oracle manipulation report](https://orochi.network/blog/oracle-manipulation-in-polymarket-2025).

---

### Pitfall 4: One-Leg Risk from Non-Atomic Cross-Platform Execution

**What goes wrong:**
The execution engine fires both legs concurrently via `asyncio.gather()` (engine.py:614-620), but there is no guarantee both fill. If one leg fills and the other fails or partially fills, you have a naked directional position instead of a hedged arbitrage. The recovery code (engine.py:692-707) only cancels pending orders -- it does NOT unwind the filled leg, leaving the operator holding directional risk.

**Why it happens:**
- Cross-platform orders are inherently non-atomic -- no mechanism exists to make Kalshi and Polymarket execute as a single transaction
- Network latency between the two API calls creates a race window
- Prices move between submission of leg 1 and leg 2
- One platform may be down, rate-limited, or in maintenance while the other processes the order
- The current code submits limit orders without `fill_or_kill` or `IOC` time-in-force -- a resting order may fill minutes later when prices have moved

**How to avoid:**
- Use `fill_or_kill` (FOK) time-in-force on Kalshi orders to guarantee immediate full fill or nothing
- For Polymarket, set tight expiration on orders (py-clob-client supports order expiration)
- Implement an active "leg monitor" that watches for fill confirmation on both legs within a configurable timeout (e.g., 5 seconds)
- If one leg fills and the other does not fill within the timeout, immediately attempt to unwind the filled leg at market -- do NOT just cancel the unfilled leg and leave a naked position
- Track the **actual fill price** (not the submitted price) to recalculate whether the arb is still profitable after fills
- For $1K capital, limit position sizes so worst-case directional exposure from a one-leg failure is survivable (e.g., max $50-100 per leg initially)

**Warning signs:**
- `status: "recovering"` executions in the dashboard with no subsequent unwind action
- Executions where one leg shows `FILLED` and the other shows `FAILED` or `CANCELLED`
- Growing directional exposure on one platform while the other platform shows zero positions

**Phase to address:**
Phase 2 (Execution Hardening) -- must be addressed before any automated trading. Manual-first execution with operator confirmation on each leg is the safe starting approach.

**Confidence:** HIGH -- this is the single most discussed failure mode in prediction market arbitrage literature, verified across [Token Metrics guide](https://tokenmetrics.com/blog/prediction-market-arbitrage/), [Alphascope guide](https://www.alphascope.app/blog/prediction-market-arbitrage-guide), and [Substack implementation writeup](https://navnoorbawa.substack.com/p/building-a-prediction-market-arbitrage).

---

### Pitfall 5: Stale Quote Trading -- Executing on Prices That No Longer Exist

**What goes wrong:**
The price store uses 30-second TTL with polling intervals of 10-30 seconds per platform. An opportunity detected at scan time may have vanished by execution time. Worse, the "best price" displayed in the order book may have been pulled by market makers in response to news, and the next available price is significantly worse.

**Why it happens:**
- Polling-based price collection (not streaming) introduces inherent latency: Kalshi 10s, Polymarket 15s, PredictIt 30s
- Quote cache TTL of 30s means you may be acting on prices that are up to 30 seconds old
- Research shows 78% of arbitrage opportunities in low-liquidity markets fail due to execution inefficiencies
- Arbitrage windows on Polymarket last an average of 2-15 seconds -- by the time a 10-15s polling cycle detects it, the opportunity may already be gone
- Order book depth can vanish instantly during volatile events -- "$5,000 of displayed depth might have only $500 of firm liquidity"

**How to avoid:**
- The existing `_pre_trade_requote()` is a good start but insufficient -- it checks the cache, not the live order book
- Before executing, fetch the **live order book** from both platforms (not the cached mid-price) and verify executable depth at the arb price
- Check `quote_age_seconds` and reject any opportunity where quotes are older than 5 seconds (not the current 30s `max_quote_age_seconds`)
- Verify `min_available_liquidity` against the intended order size -- a 7-cent spread means nothing if there is only $50 of depth
- Migrate Polymarket to WebSocket price feeds (the collector already has WebSocket support) for sub-second price updates
- Use Kalshi WebSocket feed for real-time order book updates instead of REST polling

**Warning signs:**
- High ratio of requote aborts (prices moved between scan and execution)
- Orders filling at prices significantly different from the scanned opportunity price
- Positive scanner output (many opportunities detected) but negative execution results (most fail or slip)

**Phase to address:**
Phase 2 (Execution Hardening) -- requires upgrading from polling to streaming and adding order-book-depth verification.

**Confidence:** HIGH -- verified against [Alphascope arbitrage guide](https://www.alphascope.app/blog/prediction-market-arbitrage-guide) and [stale price arbitrage analysis](https://www.linkedin.com/pulse/probabilities-stale-quotes-arbitrage-jasen-mackie).

---

### Pitfall 6: Fee Calculation Errors Turn Apparent Profits Into Actual Losses

**What goes wrong:**
The codebase uses hardcoded fee rates that do not match current platform fee schedules. Polymarket's fee rates vary by market category (crypto: 0.072, sports: 0.03, politics: 0.04, geopolitics: 0.0) with a quadratic formula `C * feeRate * p * (1-p)`, but the code uses a flat 2% default. This means the system will overestimate fees for some categories (geopolitics = free) and underestimate for others (crypto = 7.2% rate). Additionally, the code only accounts for taker fees -- it does not account for spread costs (the difference between displayed mid-price and executable bid/ask).

**Why it happens:**
- Fee structures change over time and differ by market category
- The code at `settings.py:94-98` uses static fallback rates: `politics: 0.02, sports: 0.02, crypto: 0.015`
- Polymarket's actual rates (per [official docs](https://docs.polymarket.com/trading/fees)) are: crypto: 0.072, sports: 0.03, politics: 0.04, economics: 0.05, geopolitics: 0.0
- Kalshi's 7% taker fee rate in code (`KALSHI_TAKER_FEE_RATE = 0.07`) uses the same quadratic formula and appears approximately correct, but should be verified against current docs
- PredictIt's 10% profit fee + 5% withdrawal fee is correctly modeled but the $3,500 contract limit (raised from $850 in 2025) affects position sizing math
- At 50-cent midpoint, break-even spread is roughly 2.75 cents -- any apparent arb below that is a phantom

**How to avoid:**
- Fetch fee rates from platform APIs where possible rather than hardcoding
- For Polymarket, check the `feesEnabled` flag and market category on each market before calculating fees
- Calculate fees on the **executable bid/ask price** (not the mid-price or last-trade price) -- the spread cost IS a fee
- Add a "fee verification" step that compares calculated fees against the actual fee charged on fills (Kalshi now returns `fee_cost` on each fill since January 2026)
- Set a minimum net edge threshold that provides a safety margin above break-even (e.g., require 5+ cent net edge, not just positive)

**Warning signs:**
- Scanner shows profitable opportunities but executed trades have lower or negative realized PnL
- Fee amounts on actual fills differ from pre-trade estimates by more than 10%
- Consistent small losses on trades that the scanner flagged as profitable

**Phase to address:**
Phase 1 (API Integration Audit) for fee rate updates, Phase 2 (Execution Hardening) for executable-price fee verification.

**Confidence:** HIGH for Polymarket fees (verified against [official fee docs](https://docs.polymarket.com/trading/fees)). MEDIUM for Kalshi fee rate accuracy (7% quadratic appears reasonable but needs verification against current docs).

---

### Pitfall 7: Kalshi Auth Token Expiry Causes Silent Order Failures

**What goes wrong:**
Kalshi authentication tokens expire every 30 minutes. The system uses RSA-PSS signature-based auth but if the token refresh cycle fails or drifts, all subsequent API calls silently fail. Since the execution engine creates sessions lazily (engine.py:796-799), a session that was valid during price collection may be expired by execution time.

**Why it happens:**
- Kalshi requires re-authentication every 30 minutes via the login endpoint
- The current code authenticates at startup but there is no visible token refresh loop
- Network issues during refresh create a window where the system believes it is authenticated but is not
- Price collection may succeed with cached data while order submission fails with stale auth

**How to avoid:**
- Implement a proactive token refresh that re-authenticates 2-3 minutes before expiry (not on-demand when a request fails)
- Add auth-status health checks to the readiness system that verify the token is valid before each execution cycle
- Treat any 401/403 from Kalshi as an auth expiry signal and immediately attempt refresh before retrying
- Log token refresh events and alert on failures -- auth failure should pause all Kalshi trading immediately

**Warning signs:**
- Kalshi price collection continues working but order submission fails with 401/403
- Intermittent order failures that cluster around 30-minute boundaries
- Auth status shows "authenticated" in dashboard but orders fail

**Phase to address:**
Phase 1 (API Integration Audit) -- must be verified and hardened before live trading.

**Confidence:** HIGH -- verified against [Kalshi API Keys docs](https://docs.kalshi.com/getting_started/api_keys) and [Kalshi Python client documentation](https://github.com/AndrewNolte/KalshiPythonClient).

---

## Moderate Pitfalls

### Pitfall 8: Capital Lockup Destroys Annualized Returns

**What goes wrong:**
Prediction market contracts lock capital from trade entry until event resolution. A 4% return over 3 months is only 16% annualized. With $1K per platform ($3K total), even successful arbs may lock up capital for weeks or months, preventing new trades and effectively reducing the system to a savings account with extra risk.

**Prevention:**
- Prioritize short-duration markets (resolving within days or weeks, not months)
- Track capital utilization rate: `(locked_capital / total_capital) * 100` -- alert when above 70%
- Factor annualized return into opportunity scoring, not just absolute cents of edge
- Set a maximum lockup duration per trade (e.g., reject opportunities on markets resolving >30 days out unless the edge is exceptionally large)

**Warning signs:**
- All capital locked in open positions, no new trades possible
- Many positions sitting idle waiting for resolution with declining opportunity cost

**Phase to address:**
Phase 3 (Portfolio Optimization) -- position sizing and capital allocation strategy.

---

### Pitfall 9: PredictIt Has No Trading API -- Manual Execution Only

**What goes wrong:**
PredictIt does not expose a trading API. All trades must be executed through the web interface manually. The codebase's execution engine returns `OrderStatus.FAILED` with "Unsupported auto-trading platform" for PredictIt (engine.py:679-690), but the scanner still detects PredictIt-based opportunities that require manual execution via the dashboard's manual workflow.

**Prevention:**
- Accept that PredictIt legs are manual-only and design the workflow accordingly
- The existing manual position workflow (engine.py:336-398) is the correct approach
- Ensure the Telegram alert for PredictIt opportunities includes all necessary trade details (market URL, exact price, quantity) so the operator can execute quickly
- Consider time-sensitivity: by the time a human acts on a PredictIt alert, the Kalshi/Polymarket price may have moved
- New PredictIt contract limit is $3,500 (raised from $850 in July 2025) -- update any hardcoded limits

**Warning signs:**
- PredictIt opportunities detected but never executed because manual workflow is too slow
- PredictIt-involved arbs showing in "recovering" status because the automated leg executed but the manual leg was never entered

**Phase to address:**
Phase 1 (already partially addressed with manual workflow) -- verify the workflow works end-to-end.

---

### Pitfall 10: Kalshi Maintenance Windows Kill Open Orders

**What goes wrong:**
Kalshi has scheduled maintenance from 3:00-5:00 AM ET on Thursdays. During maintenance, all trading halts and API access is unavailable. Any resting orders or pending executions during this window are in limbo. If one leg of an arb filled just before maintenance and the other is resting, you have naked directional exposure for 2+ hours.

**Prevention:**
- Check Kalshi exchange status via the `GET /exchange/status` endpoint before any execution
- Cancel all resting Kalshi orders 15 minutes before scheduled maintenance
- Do NOT submit new orders within 30 minutes of known maintenance windows
- Monitor [kalshistatus.com](https://kalshistatus.com/) for unscheduled downtime
- Store maintenance schedule in config and enforce trading blackout periods

**Warning signs:**
- Orders submitted near 3 AM ET on Thursdays failing silently
- Resting orders from before maintenance still "pending" after maintenance ends (may need re-submission)

**Phase to address:**
Phase 2 (Execution Hardening) -- add maintenance-window awareness.

---

### Pitfall 11: Polymarket Gas Token (POL/MATIC) Balance Depletion

**What goes wrong:**
Polymarket runs on Polygon and requires POL/MATIC for gas fees. While fees per transaction are fractions of a cent, a bot executing hundreds of trades can deplete the gas balance. If the gas token runs out, all Polymarket transactions fail even though the USDC balance is sufficient.

**Prevention:**
- Monitor POL/MATIC balance alongside USDC balance in the balance monitor
- Set a minimum POL/MATIC threshold (e.g., $5 worth) and alert when below
- Keep $10-20 of POL/MATIC on the wallet at all times (enough for thousands of transactions)
- Automate gas token replenishment or at minimum alert for manual top-up

**Warning signs:**
- Polymarket orders failing with gas-related errors while USDC balance shows healthy
- Slowly declining POL/MATIC balance approaching zero

**Phase to address:**
Phase 2 (Execution Hardening) -- add to balance monitoring.

---

### Pitfall 12: Rate Limit Exhaustion During High-Volatility Events

**What goes wrong:**
Kalshi basic tier allows 20 reads/second and 10 writes/second. During breaking news events (when arbitrage opportunities are most abundant), the system may exhaust rate limits by polling for price updates, causing it to miss opportunities or fail to execute. Polymarket CLOB has its own rate limits that trigger backoff (already partially handled in the collector).

**Prevention:**
- Implement token-bucket rate limiting per platform (not just backoff-on-429)
- Prioritize write requests (orders) over read requests (price polling) during execution windows
- Batch cancel operations use 0.2 transactions per cancel on Kalshi -- prefer batch cancels over individual
- Consider applying for Kalshi Advanced tier (30/30 rps) if trading volume justifies it
- For Polymarket, the existing `_fetch_book` backoff handling is a good start but needs to also protect the order submission path

**Warning signs:**
- 429 responses increasing during volatile market conditions
- Price data becoming stale (cache misses) during periods when opportunities should be most abundant

**Phase to address:**
Phase 2 (Execution Hardening) -- implement proper rate limiting.

**Confidence:** HIGH -- verified against [Kalshi Rate Limits docs](https://docs.kalshi.com/getting_started/rate_limits).

---

## Minor Pitfalls

### Pitfall 13: Demo Environment Gives False Confidence

**What goes wrong:**
Kalshi's demo environment (`demo-api.kalshi.co`) mirrors production but uses separate credentials and fake money. Passing all tests in demo does NOT guarantee production success because: (a) demo may lag behind production API changes, (b) demo order books have different liquidity, (c) demo does not enforce the same rate limits under load.

**Prevention:**
- Use demo for structural validation only (auth flow, endpoint paths, response parsing)
- Plan for a separate "production smoke test" phase with minimum-size real-money orders ($1 contracts)
- Verify that the demo API version matches the production API version before trusting demo results

**Phase to address:**
Phase 1 (API Integration Audit) -- demo testing, then Phase 2 (Live Smoke Testing).

---

### Pitfall 14: Tax Reporting Complexity for Cross-Platform Arbitrage

**What goes wrong:**
The IRS has not issued formal guidance on prediction market tax treatment. Kalshi issues 1099-B forms but Polymarket does not issue 1099s. Traders must choose between Section 1256 (60/40 capital gains split), gambling income (Schedule C), or ordinary income -- and the choice has significant tax implications. Cross-platform arb trades create complex cost-basis tracking.

**Prevention:**
- Log every trade with full cost basis, fees, and settlement amounts from day one
- Treat this as ordinary income for conservative tax planning until formal guidance exists
- Consult a CPA familiar with prediction markets before filing
- The existing trade logging and P&L reconciliation infrastructure is a strong foundation -- ensure it captures enough detail for tax reporting

**Phase to address:**
Phase 3 (Production Operations) -- operational concern, not a code blocker.

---

### Pitfall 15: No Kill Switch for Runaway Trading

**What goes wrong:**
The current codebase has graceful shutdown via SIGINT/SIGTERM but no instant kill switch accessible from the dashboard. If the system enters a loop of bad trades (e.g., executing on stale prices, fee miscalculation), there is no way to halt all trading immediately from the monitoring interface without SSH access to stop the container.

**Prevention:**
- Add a dashboard "HALT ALL TRADING" button that sets a global trading-paused flag
- Implement a Telegram command that triggers the same halt
- Add automatic circuit breakers: halt after N consecutive failed executions, halt if realized PnL drops below threshold, halt if daily loss exceeds limit
- The circuit breaker on collectors exists but there is no equivalent on the execution engine

**Phase to address:**
Phase 2 (Execution Hardening) -- must exist before automated trading.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Hardcoded fee rates | Quick implementation | Silent P&L erosion when rates change | Never in production -- must fetch or validate dynamically |
| Polling instead of WebSocket for prices | Simpler implementation | Stale data, missed opportunities | Only during initial testing with manual execution |
| No order book depth verification | Faster scanning | Executing on phantom liquidity | Never for automated execution |
| In-memory execution state | Fast, no DB overhead | Lost state on restart, no audit trail | Only during dry-run testing |
| `asyncio.gather()` without individual timeouts | Simpler concurrent code | One hung leg blocks the entire execution | Never for production order submission |
| Single `aiohttp.ClientSession` for all platforms | Less resource usage | One platform's slow response blocks others | Never in production -- use per-platform sessions |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Kalshi Orders | Using `yes_price` in integer cents (removed March 2026) | Use `yes_price_dollars` as a string (e.g., `"0.65"`) |
| Kalshi Orders | Submitting `count` as integer for fractional markets | Check `fractional_trading_enabled` and use `count_fp` |
| Kalshi Auth | Assuming auth token persists indefinitely | Re-authenticate every 25-28 minutes proactively |
| Kalshi Demo vs Prod | Trusting demo results translate to production | Different endpoints, credentials, and potentially API versions |
| Polymarket Client | Not setting `signature_type` and `funder` | Always specify signature_type (0, 1, or 2) and funder address |
| Polymarket Orders | Using mid-price for order sizing | Use best bid/ask from the CLOB -- mid-price is not executable |
| Polymarket Fees | Flat fee rate for all markets | Category-specific rates: crypto=0.072, sports=0.03, politics=0.04, geopolitics=0.0 |
| Polymarket Fees | Not checking `feesEnabled` flag | Pre-fee-activation markets charge zero fees |
| PredictIt | Attempting API-based trading | No trading API exists -- web interface only |
| PredictIt | Using old $850 contract limit | Limit raised to $3,500 in July 2025 |
| All Platforms | Treating market titles as resolution criteria | Full resolution criteria must be read and compared manually |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Polling all markets at fixed intervals | Rate limit exhaustion during events | Adaptive polling -- increase frequency for mapped markets only | >50 tracked markets on Kalshi basic tier |
| Scanning all market pairs on every price update | CPU spike, execution delay | Only rescan pairs where a price actually changed | >100 mapped market pairs |
| Unbounded execution history in memory | Memory growth, slower dashboard | Move to PostgreSQL after initial fill, keep only last 100 in memory | >500 executions per session |
| Single-threaded event loop for all platforms | One slow platform blocks everything | Per-platform asyncio tasks with independent timeouts | When any platform API responds >5s |
| Re-fetching full order book for depth checks | Rate limit consumption | Cache order book with short TTL (2-3s), only refetch on execution | Frequent execution attempts (>1 per minute) |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| API keys in .env without file permissions | Key theft if system is compromised | chmod 600 on .env, use Docker secrets in production |
| RSA private key in filesystem (arbiter/keys/) | Full account takeover if key is leaked | Verify .gitignore covers keys/, consider encrypted key storage |
| Polymarket private key = full wallet control | Entire wallet balance at risk | Use a dedicated wallet with only trading capital, not a personal wallet |
| No IP allowlisting on platform API access | Stolen keys work from anywhere | Configure IP restrictions on Kalshi API keys if available |
| Dashboard auth over HTTP | Session hijacking on network | Use HTTPS with proper TLS certificates, even for local access |
| Logging raw API responses that may contain keys | Credentials in log files | Sanitize log output to strip auth headers and key material |

## "Looks Done But Isn't" Checklist

- [ ] **Kalshi order submission:** Code exists but uses removed integer-cent format -- verify orders actually submit and fill on production API
- [ ] **Polymarket order submission:** Code exists but may fail auth due to missing signature_type/funder -- verify a real order posts and fills
- [ ] **Fee calculations:** Code exists but rates don't match current platform schedules -- verify calculated fees match actual fees on fills
- [ ] **Re-quote check:** Checks cached prices but not live order book depth -- verify executable liquidity exists at the arb price
- [ ] **One-leg recovery:** Cancels unfilled leg but does not unwind filled leg -- verify the operator has a workflow to exit naked positions
- [ ] **Kill switch:** Graceful shutdown exists but no dashboard-accessible emergency halt -- verify trading can be stopped instantly without SSH
- [ ] **Market mapping:** Text similarity scoring exists but resolution criteria comparison does not -- verify mapped markets actually resolve identically
- [ ] **Balance monitoring:** Balance fetch exists but does not check Polymarket gas token balance -- verify POL/MATIC monitoring is included
- [ ] **Token refresh:** Auth initialization exists but periodic refresh may not -- verify Kalshi auth survives >30 minutes of continuous operation
- [ ] **PredictIt contract limit:** May still use old $850 limit -- verify code reflects $3,500 limit

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Kalshi price format rejection | LOW | Update order body fields from integer cents to dollar strings; test in demo; deploy |
| Polymarket auth failure | LOW | Add signature_type and funder to client init; re-derive API creds; test |
| Settlement divergence loss | HIGH | No code fix possible -- financial loss is realized. Prevent by auditing resolution criteria before mapping. Review and restrict market pairs. |
| One-leg naked position | MEDIUM-HIGH | Immediately unwind the filled leg at market price (accepting slippage); add automated unwind logic for future prevention |
| Stale quote execution | MEDIUM | Cancel unfilled orders; if filled, assess P&L impact; tighten quote age limits and add depth verification |
| Fee miscalculation losses | LOW-MEDIUM | Update fee rates; recalculate P&L on past trades; adjust minimum edge threshold |
| Auth token expiry | LOW | Implement proactive refresh; restart system to force re-auth; no financial loss if orders were not submitted |
| Capital lockup | MEDIUM | Cannot accelerate market resolution; can exit positions early by selling contracts at market price (likely at a loss) |
| Rate limit exhaustion | LOW | Implement proper rate limiting; reduce polling frequency; apply for higher tier |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Kalshi price format (P1) | Phase 1: API Audit | Submit and fill a real $1 order on Kalshi production |
| Polymarket auth (P2) | Phase 1: API Audit | Submit and fill a real $1 order on Polymarket production |
| Resolution divergence (P3) | Phase 1: Market Mapping Audit | All mapped pairs have resolution criteria documented and compared |
| One-leg risk (P4) | Phase 2: Execution Hardening | Simulate one-leg failures and verify unwind logic activates |
| Stale quotes (P5) | Phase 2: Execution Hardening | Verify depth check rejects thin-book opportunities |
| Fee miscalculation (P6) | Phase 1: API Audit + Phase 2 | Executed fees match pre-trade estimates within 5% |
| Auth token expiry (P7) | Phase 1: API Audit | System runs >2 hours without auth failures |
| Capital lockup (P8) | Phase 3: Portfolio Optimization | Capital utilization tracking and duration-aware scoring |
| PredictIt manual workflow (P9) | Phase 1: Verify existing | End-to-end manual trade via dashboard Telegram alert flow |
| Maintenance windows (P10) | Phase 2: Execution Hardening | System pauses trading during known maintenance |
| Gas token depletion (P11) | Phase 2: Execution Hardening | Balance monitor includes POL/MATIC with alert threshold |
| Rate limiting (P12) | Phase 2: Execution Hardening | System handles 429 responses gracefully without lost orders |
| Demo false confidence (P13) | Phase 1: API Audit | Explicit production smoke test with real money |
| Tax reporting (P14) | Phase 3: Production Ops | Trade logs contain sufficient detail for 1099 reconciliation |
| No kill switch (P15) | Phase 2: Execution Hardening | Dashboard halt button stops all trading within 1 second |

## Sources

- [Kalshi API Changelog](https://docs.kalshi.com/changelog) -- March 2026 breaking changes (HIGH confidence)
- [Kalshi Rate Limits](https://docs.kalshi.com/getting_started/rate_limits) -- tier-based rate limiting (HIGH confidence)
- [Kalshi API Keys / Auth](https://docs.kalshi.com/getting_started/api_keys) -- RSA-PSS signing, 30-min token expiry (HIGH confidence)
- [Kalshi Demo Environment](https://docs.kalshi.com/getting_started/demo_env) -- demo vs production differences (HIGH confidence)
- [Kalshi Exchange Status](https://docs.kalshi.com/api-reference/exchange/get-exchange-status) -- maintenance windows (HIGH confidence)
- [Polymarket Fee Structure](https://docs.polymarket.com/trading/fees) -- category-based taker fees (HIGH confidence)
- [Polymarket Authentication](https://docs.polymarket.com/api-reference/authentication) -- signature types, funder address (HIGH confidence)
- [py-clob-client Issue #187](https://github.com/Polymarket/py-clob-client/issues/187) -- 401 on post_order() (HIGH confidence)
- [py-clob-client Issue #278](https://github.com/Polymarket/py-clob-client/issues/278) -- L2 auth failures (HIGH confidence)
- [py-clob-client Issue #185](https://github.com/Polymarket/py-clob-client/issues/185) -- sell market order issues (HIGH confidence)
- [py-clob-client Issue #182](https://github.com/Polymarket/py-clob-client/issues/182) -- pagination failures >500 orders (MEDIUM confidence)
- [DeFi Rate: How Contracts Settle](https://defirate.com/prediction-markets/how-contracts-settle/) -- settlement divergence examples (HIGH confidence)
- [Orochi: Oracle Manipulation in Polymarket 2025](https://orochi.network/blog/oracle-manipulation-in-polymarket-2025) -- UMA whale manipulation (HIGH confidence)
- [Finance Magnates: Iran Market Split](https://www.financemagnates.com/cryptocurrency/us-military-action-against-iran-exposes-split-between-polymarket-and-kalshi-models/) -- Khamenei resolution divergence (HIGH confidence)
- [Token Metrics: Prediction Market Arbitrage Guide](https://tokenmetrics.com/blog/prediction-market-arbitrage/) -- execution risk, capital lockup (MEDIUM confidence)
- [Alphascope: Prediction Market Arbitrage Guide](https://www.alphascope.app/blog/prediction-market-arbitrage-guide) -- stale prices, depth verification (MEDIUM confidence)
- [Substack: Building a Prediction Market Arbitrage Bot](https://navnoorbawa.substack.com/p/building-a-prediction-market-arbitrage) -- implementation gotchas (MEDIUM confidence)
- [NYC Servers: Prediction Market Arbitrage Guide](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide) -- fee break-even analysis (MEDIUM confidence)
- [PredictIt 2026 Overview](https://marvn.ai/discover/guides/predictit-overview) -- $3,500 limit, no trading API (MEDIUM confidence)
- [Prediction Market Tax Guide](https://www.alphascope.app/blog/prediction-market-tax-guide) -- tax reporting complexity (MEDIUM confidence)

---
*Pitfalls research for: cross-platform prediction market arbitrage (Kalshi, Polymarket, PredictIt)*
*Researched: 2026-04-16*
