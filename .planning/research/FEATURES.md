# Feature Research

**Domain:** Cross-platform prediction market arbitrage (Kalshi, Polymarket, PredictIt)
**Researched:** 2026-04-16
**Confidence:** HIGH

## Feature Landscape

### Table Stakes (Must Have or You Lose Money)

These are non-negotiable for live trading. Missing any one of these can cause direct capital loss.

#### Safety & Risk Controls

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Kill switch (emergency halt) | One-click or automatic halt of all trading activity. Without this, a bug or market event causes uncontrolled losses. Knight Capital lost $440M in 45 minutes without one. | MEDIUM | Must halt new orders AND cancel all open/pending orders. Needs both dashboard button and programmatic trigger (loss threshold, error rate). Existing engine has `_running` flag but no true kill switch that cancels in-flight orders. |
| Daily loss limit | Caps cumulative daily losses. Prevents compounding errors from draining capital across many small bad trades. | LOW | Existing `RiskManager` has `_max_daily_loss = -50.0`. Verify it actually blocks trades when hit and cannot be circumvented. Needs reset logic (when does "daily" reset?). |
| Per-trade position limits | Prevents any single trade from risking too much capital. Essential with small capital ($1K/platform). | LOW | Existing `max_position_usd` in scanner config. Verify enforcement path from scanner through execution. |
| Total exposure cap | Prevents over-leveraging across all open positions. Capital is locked until settlement (weeks/months). | LOW | Existing `_max_total_exposure = 500.0` in RiskManager. Must be verified against live balance data, not just in-memory tracking. |
| Stale price detection | Prices older than TTL must never trigger execution. Trading on stale data is the most common cause of losses in automated systems. | MEDIUM | Existing 30s TTL in PriceStore and `max_quote_age_seconds` check in RiskManager. Must verify TTL is enforced at the execution moment, not just scan time. Need staleness alerting when a platform stops updating. |
| Pre-trade re-quote | Re-fetch prices immediately before order submission. The price you scanned may have moved by the time you execute. | MEDIUM | Existing `_pre_trade_requote()` in engine. Must verify it actually hits live APIs (not just cache) and aborts if spread has collapsed. |
| Partial fill / leg risk handling | When one leg fills but the other doesn't, you have a naked directional position. Must detect, alert, and provide recovery workflow. | HIGH | Existing engine has concurrent leg execution and re-quote checks. But recovery from a one-legged position (the hardest case) needs explicit handling: cancel unfilled leg, alert operator, track exposure of naked position. This is the most dangerous failure mode in cross-platform arbitrage. |
| Order cancellation | Must be able to cancel submitted but unfilled orders. Without this, you cannot recover from leg risk or kill switch scenarios. | MEDIUM | Requires verified cancel API calls per platform. Kalshi and Polymarket both support cancel. PredictIt cancel is manual (browser). Must handle "cancel rejected" (already filled). |
| Rate limit compliance | Each platform has strict rate limits. Exceeding them gets you throttled or banned. Kalshi counts batch items individually. | LOW | Must implement per-platform rate limiters. Kalshi has tiered limits. Polymarket ~100+ req/min authenticated. Not just "don't exceed" but backoff and queue when approaching limits. |
| Platform authentication refresh | Kalshi tokens expire every 30 minutes. Sessions must auto-refresh without interrupting trading. | MEDIUM | Current Kalshi collector has auth but must verify it handles token expiry mid-session gracefully. Polymarket uses long-lived API keys. PredictIt sessions may expire. |
| Settlement rule verification | Different platforms can resolve the same event differently (proven: 2024 government shutdown -- Polymarket YES, Kalshi NO for same event). Must verify resolution criteria match before trading. | HIGH | Currently handled in market mapping (candidate -> review -> confirmed workflow). But needs deeper verification: compare actual settlement language, not just event names. This is an existential risk -- you can have perfect execution and still lose on divergent settlement. |

#### Operational Fundamentals

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Dry-run mode | Must be able to run the full pipeline without placing real orders. Essential for testing and confidence-building. | LOW | Existing `dry_run` flag in scanner config. TypeScript CLI also has dry-run. Verify the Python live path simulates correctly (same code path minus API calls). |
| Sandbox/demo testing | Test against real platform APIs with fake money before going live. Kalshi has an explicit demo environment (demo-api.kalshi.co). | MEDIUM | Kalshi demo env uses separate credentials and mock funds. Polymarket has no official sandbox -- must test with minimal real capital on mainnet. PredictIt has no sandbox. System needs config to switch between demo and prod endpoints per platform. |
| Audit trail / trade logging | Every decision (scan, reject, execute, fill, cancel, incident) must be logged with timestamps and full context. Required for debugging, reconciliation, and learning. | MEDIUM | Existing TradeLogger writes JSON. Existing incident tracking. Must persist to PostgreSQL (not just in-memory deques). In-memory-only means a crash loses your trade history. |
| Balance reconciliation | Compare recorded P&L against actual platform balances. Discrepancies indicate bugs, missed fills, or fee miscalculations. | MEDIUM | Existing `pnl_reconciler.py` and `math_auditor.py`. Must verify against live API balance queries, not just internal state. |
| Real-time dashboard | Monitor prices, opportunities, executions, balances, and incidents as they happen. 24/7 operation requires visual monitoring. | LOW | Existing WebSocket dashboard with all views. Already built and functional. |
| Telegram/mobile alerts | Critical events (executions, incidents, low balance, kill switch triggers) must reach you when away from dashboard. Markets operate 24/7. | LOW | Existing TelegramNotifier. Verify it works end-to-end. Add alerts for: kill switch activation, one-legged position, reconciliation mismatch, platform API down. |
| Graceful shutdown | SIGINT/SIGTERM must not leave orders in unknown state. Must cancel pending orders, log final state, and persist all data before exit. | MEDIUM | Existing signal handlers in main.py. Must verify they actually cancel open orders (not just stop the event loop). Unclean shutdown with open orders is a capital risk. |
| Fee-accurate profit calculation | Fees vary by platform, market, and position (Kalshi quadratic, Polymarket 2% on winners, PredictIt 10% profit + 5% withdrawal). Incorrect fee math means what looks profitable isn't. | MEDIUM | Existing fee functions per platform. Must verify accuracy against actual platform fee schedules (these change). Cross-reference with actual fills to confirm math matches reality. |

### Differentiators (Competitive Advantage)

Features that improve profitability or reduce operational burden beyond minimum viability.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Persistence gating (consecutive-scan confirmation) | Requires an opportunity to appear N consecutive scans before execution. Filters out noise and transient price glitches. Reduces false positive trades. | LOW | Already implemented in scanner. Tunable parameter. Unique advantage over simpler bots that execute on first sight. |
| Automated market mapping with curation | Automatically match markets across platforms by name/description similarity, then let operator confirm. Reduces manual work of finding arbitrage pairs. | MEDIUM | Existing mapping layer with scoring and status workflow. The curation UI (candidate -> review -> confirmed) is a differentiator because most hobbyist bots require manual pair configuration. |
| Capital lockup / annualized return calculation | Show not just the nominal spread but the annualized return based on time-to-settlement. A 4% spread on a 3-month market is ~16% annualized; same spread on a 1-week market is ~200% annualized. Helps prioritize which opportunities to pursue. | LOW | Existing settlement calendar tracking in portfolio monitor. Add annualized return display to opportunity ranking. |
| Liquidity-aware position sizing | Check order book depth before execution and size positions to avoid moving the market. $1K in an illiquid market may only fill $100 at displayed price. | MEDIUM | Not currently implemented. Pre-trade should query order book depth (Kalshi provides this; Polymarket CLOB has it). Size position to X% of available liquidity. |
| Multi-strategy opportunity detection | Beyond simple YES+NO=<$1.00 across platforms: detect complement sets, partition arbitrage, and same-platform YES+NO mispricing. | MEDIUM | Current scanner does cross-platform spread detection. More sophisticated constraint families (partitions, mutual exclusion) could find opportunities others miss. |
| Automated position unwinding | When conditions change (spread collapses, settlement approaches, kill switch fires), automatically attempt to close positions at minimal loss rather than just alerting. | HIGH | Current system alerts but requires manual intervention. Automated unwinding adds complexity (needs to handle illiquidity, partial unwinds, cross-platform coordination). Defer until manual workflow is proven reliable. |
| WebSocket price streaming | Use platform WebSocket feeds instead of REST polling for lower latency price updates. Faster detection = higher chance of capturing spreads. | MEDIUM | Current collectors use REST polling (10s/15s/30s intervals). Kalshi and Polymarket both offer WebSocket feeds. Significant latency improvement but adds reconnection complexity. |
| Execution latency tracking | Measure and display time from opportunity detection to order fill. Identifies when infrastructure is too slow to capture spreads. | LOW | Not currently tracked. Add timestamps at each pipeline stage (scan_ts, decision_ts, submit_ts, fill_ts). Reveals if your edge is being eaten by latency. |
| Platform health monitoring | Track API response times, error rates, and availability per platform. Detect degradation before it causes failed trades. | LOW | Existing circuit breaker pattern on collectors. Enhance with response time percentiles and availability metrics. Dashboard display of platform health status. |

### Anti-Features (Commonly Requested, Often Problematic)

Features that seem appealing but create more problems than they solve, especially at this stage.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Full automation (no human oversight) | "Set it and forget it" appeal. Run while sleeping. | At $1K/platform with untested code, full automation is how you lose everything to a single bug. The 2024 Polymarket/Kalshi settlement divergence proves human judgment is needed for edge cases. Knight Capital's fully automated system lost $440M. | Semi-automated: system detects and proposes, operator confirms or auto-executes only within strict limits. Graduate to fuller automation after proven track record. |
| AI/ML price prediction | "Predict where prices will move." Sounds like alpha. | Prediction markets already embed collective intelligence. You're not trading mispricing within a market -- you're capturing cross-platform spread. ML adds complexity without addressing the core value (spread capture, not directional betting). | Focus on execution speed and fee optimization. The edge is in the spread, not prediction. |
| High-frequency trading infrastructure | Sub-millisecond execution, colocation, FIX protocol. | Prediction market spreads persist for seconds to minutes, not microseconds. HFT infrastructure costs thousands/month and optimizes for a problem you don't have. Kalshi FIX protocol requires highest API tier approval. | REST API with 1-5 second execution is sufficient. Most prediction market arb opportunities last much longer than equity HFT windows. |
| Backtesting engine | "Test strategies against historical data." Sounds responsible. | Prediction market historical data is sparse and unreliable. Order book depth history doesn't exist. Backtesting gives false confidence because it can't simulate execution reality (slippage, partial fills, settlement differences). | Forward-test with minimal real capital ($50-100 per trade). Real execution reveals problems backtesting hides. PROJECT.md already correctly scopes this out. |
| Multi-user support | "Let friends use it too." | Adds authentication complexity, permission models, capital isolation, and liability. Single-operator system is dramatically simpler. | Keep single-user. If others want to use it, they run their own instance. |
| Additional platforms (beyond Kalshi/Polymarket/PredictIt) | "More platforms = more opportunities." | Each platform adds authentication complexity, fee models, settlement rules, and API quirks. Stabilize three platforms first. PredictIt is winding down anyway. | Master Kalshi + Polymarket. Add platforms only after core is profitable and stable. PROJECT.md correctly scopes this out. |
| Aggressive position sizing / auto-scaling | "Scale up when it's working." | With $1K/platform, aggressive sizing means one bad trade wipes you out. Auto-scaling amplifies both wins AND bugs. | Fixed conservative sizing until 3+ months of profitable operation. Manual capital allocation increases. |
| Real-time P&L push notifications | "Tell me every time I make money." | Notification fatigue. Constant alerts for $2-5 profits desensitize you to the alerts that actually matter (incidents, kill switch, reconciliation failures). | Alert on incidents and violations only. P&L is visible on dashboard for when you want to check it. |

## Feature Dependencies

```
[Platform Authentication]
    |
    v
[Price Collection (REST polling)]
    |
    +---> [Stale Price Detection]
    |         |
    |         v
    +---> [Arbitrage Scanning]
    |         |
    |         +---> [Persistence Gating]
    |         |
    |         v
    |     [Pre-Trade Re-quote]
    |         |
    |         v
    |     [Risk Gate Checks]
    |         |
    |         +---> [Daily Loss Limit]
    |         +---> [Position Limits]
    |         +---> [Total Exposure Cap]
    |         |
    |         v
    |     [Order Execution (concurrent legs)]
    |         |
    |         +---> [Order Cancellation] ---> [Kill Switch]
    |         +---> [Partial Fill Detection] ---> [Leg Risk Recovery]
    |         |
    |         v
    |     [Audit Trail / Trade Logging]
    |         |
    |         v
    |     [Balance Reconciliation]
    |
    v
[Settlement Rule Verification] (independent, must happen BEFORE first trade of any pair)

[Telegram Alerts] --enhances--> [Kill Switch], [Leg Risk Recovery], [Balance Reconciliation]

[Dashboard] --enhances--> [All monitoring features]

[Sandbox/Demo Testing] --must precede--> [Live Order Execution]

[Liquidity-Aware Sizing] --enhances--> [Risk Gate Checks]

[WebSocket Streaming] --replaces--> [REST Polling] (future upgrade, not dependency)
```

### Dependency Notes

- **Order Execution requires Platform Authentication:** Cannot submit orders without valid, non-expired auth tokens. Kalshi tokens expire every 30 minutes.
- **Kill Switch requires Order Cancellation:** A kill switch that can't cancel open orders is useless. Must verify cancel works per platform before enabling live trading.
- **Leg Risk Recovery requires Partial Fill Detection:** You can't recover from a one-legged position if you don't know you're in one. Detection must be real-time (not discovered at next reconciliation cycle).
- **Live Execution requires Sandbox Testing:** Never place the first real order on a platform without having successfully tested in sandbox (Kalshi) or with minimal capital (Polymarket).
- **Arbitrage Scanning requires Settlement Rule Verification:** The scanning layer identifies cross-platform pairs, but execution must not proceed until an operator has verified that settlement rules match between platforms for that specific pair.
- **Balance Reconciliation requires Audit Trail:** You can't reconcile if you don't have a record of what trades you think you made vs. what the platform shows.

## MVP Definition

### Launch With (v1) -- First Live Dollar

Minimum set to place a single live arbitrage trade without losing money to system failure.

- [ ] **Kill switch** -- Emergency halt that cancels open orders. Without this, any bug is unrecoverable.
- [ ] **Verified platform auth (Kalshi + Polymarket)** -- Confirmed working auth that handles token refresh. Tested in sandbox/demo first.
- [ ] **Verified order placement + cancellation** -- Confirmed that orders actually submit and cancel on each platform. Not simulated.
- [ ] **Stale price rejection** -- Quotes older than threshold are hard-rejected from execution pipeline.
- [ ] **Pre-trade re-quote** -- Fresh price check immediately before order submission, abort if spread collapsed.
- [ ] **Daily loss limit enforcement** -- Hard stop at configurable daily loss. Cannot be bypassed.
- [ ] **Partial fill detection + alerting** -- Detect one-legged positions and immediately alert operator via Telegram.
- [ ] **Rate limit compliance** -- Per-platform rate limiters preventing API bans.
- [ ] **Audit trail to PostgreSQL** -- Every trade decision persisted (not just in-memory deques).
- [ ] **Settlement rule verification for first pairs** -- Manually verified that resolution criteria match for initial trading pairs.
- [ ] **Graceful shutdown with order cancellation** -- SIGTERM cancels open orders before process exits.
- [ ] **Dry-run validation** -- Full pipeline run in dry-run mode producing realistic simulated results before enabling live.

### Add After Validation (v1.x) -- First Profitable Week

Features to add once the core pipeline has executed real trades without incidents.

- [ ] **Liquidity-aware position sizing** -- Trigger: first trade that experiences significant slippage due to thin order book.
- [ ] **Annualized return display** -- Trigger: operator needs to prioritize among multiple simultaneous opportunities.
- [ ] **Execution latency tracking** -- Trigger: suspicion that opportunities are being missed due to slow execution.
- [ ] **Platform health dashboard** -- Trigger: want to understand why scan cycles are occasionally empty.
- [ ] **Automated balance reconciliation loop** -- Trigger: manual reconciliation confirms the automated math is correct.
- [ ] **PredictIt manual workflow** -- Trigger: PredictIt opportunities appear but require browser-based execution.

### Future Consideration (v2+) -- Proven Profitable System

Features to defer until the system has demonstrated consistent profitability over weeks.

- [ ] **WebSocket price streaming** -- Why defer: REST polling at 10-15s intervals is sufficient for prediction market speed. WebSocket adds reconnection complexity.
- [ ] **Multi-strategy detection (partitions, complements)** -- Why defer: simple cross-platform YES/NO spread is the proven model. Add complexity only when simple opportunities dry up.
- [ ] **Automated position unwinding** -- Why defer: manual unwinding builds understanding of failure modes before automating them.
- [ ] **Expanded platform support** -- Why defer: each platform is weeks of integration work. Master two platforms first.

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Kill switch | HIGH | MEDIUM | P1 |
| Verified order placement/cancel | HIGH | MEDIUM | P1 |
| Stale price rejection | HIGH | LOW | P1 |
| Pre-trade re-quote (verified) | HIGH | LOW | P1 |
| Daily loss limit (verified) | HIGH | LOW | P1 |
| Partial fill detection + alert | HIGH | HIGH | P1 |
| Rate limit compliance | HIGH | LOW | P1 |
| Auth token refresh (Kalshi 30-min) | HIGH | MEDIUM | P1 |
| Settlement rule verification | HIGH | MEDIUM | P1 |
| Audit trail to PostgreSQL | HIGH | MEDIUM | P1 |
| Graceful shutdown + cancel | HIGH | MEDIUM | P1 |
| Sandbox/demo testing | HIGH | LOW | P1 |
| Liquidity-aware sizing | MEDIUM | MEDIUM | P2 |
| Execution latency tracking | MEDIUM | LOW | P2 |
| Platform health dashboard | MEDIUM | LOW | P2 |
| Annualized return display | MEDIUM | LOW | P2 |
| Balance reconciliation loop | MEDIUM | MEDIUM | P2 |
| WebSocket streaming | MEDIUM | HIGH | P3 |
| Multi-strategy detection | MEDIUM | MEDIUM | P3 |
| Automated unwinding | MEDIUM | HIGH | P3 |

**Priority key:**
- P1: Must have before first live trade (money-safety features)
- P2: Add after first profitable week (optimization features)
- P3: Future consideration after proven profitability (scaling features)

## Competitor / Reference System Analysis

| Feature | Production Kalshi Bot (danielsilvaperez/trading-core) | Polymarket Trading Infra (yussypu) | Arbiter (Current State) | Our Plan |
|---------|-------------------------------------------------------|-------------------------------------|------------------------|----------|
| Kill switch | 3-level circuit breaker (balance floor, loss streak, position limit) | `trading_enabled` flag + `round_ambiguous` flag -- both must be true | `_running` flag only; no order cancellation on halt | Implement true kill switch: halt + cancel all open orders + alert |
| Partial fill handling | Not documented | Asset isolation -- failures contained per instrument | Concurrent legs with re-quote check, but no explicit one-leg recovery | Add detection, alerting, and manual recovery workflow |
| Rate limiting | Tiered Kalshi compliance | Exponential backoff on 429s | Not explicitly implemented | Per-platform rate limiter with queue and backoff |
| Reconnection | Not documented | WebSocket resilience with 100 retries + REST reconciliation after reconnect | Circuit breaker on collector failures | Add REST reconciliation after any reconnection event |
| Audit trail | BTC price circuit breaker with fat-finger detection | Event system with MarketEvent variants | In-memory deques (lost on crash) | Persist all events to PostgreSQL |
| Settlement verification | Single-market (BTC only, no cross-platform) | Single-platform (Polymarket only) | Market mapping with confirmation workflow | Enhance with explicit resolution-rule comparison per pair |

## Existing Codebase Gap Assessment

Many of the table-stakes features already exist in code but have never been tested against live APIs. The critical gap is not "build from scratch" but "verify what exists actually works."

| Feature | Code Exists? | Verified Against Live API? | Gap |
|---------|-------------|---------------------------|-----|
| Price collection | Yes (3 collectors) | No | Must test each collector against real API |
| Arbitrage scanning | Yes | No (only with simulated data) | Verify fee calculations match actual platform fees |
| Order execution | Yes (engine.py) | No | Core risk -- does `_live_execution()` actually work? |
| Order cancellation | Partially | No | Must verify cancel API per platform |
| Kill switch | Partial (_running flag) | No | Missing: cancel open orders, Telegram alert, dashboard button |
| Risk manager | Yes | No | Verify limits actually block trades in live path |
| Pre-trade re-quote | Yes | No | Verify it queries live prices, not just cache |
| Balance monitoring | Yes | No | Verify API balance queries return correct values |
| Reconciliation | Yes | No | Cannot verify until real trades exist to reconcile |
| Telegram alerts | Yes | No | Verify bot token and chat_id work |
| Dashboard | Yes | Partially (UI works, data is simulated) | Works but shows simulated data |
| Auth refresh | Unclear | No | Kalshi 30-min token expiry is a known risk |
| Rate limiting | No explicit implementation | N/A | Must build per-platform rate limiters |
| PostgreSQL persistence | Schema exists | No | Verify write path from execution to DB |

## Sources

- [Prediction Market Arbitrage Guide 2026](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide) -- comprehensive production requirements, fee structures, infrastructure specs
- [Polymarket Trading Infrastructure - Error Handling & Recovery](https://deepwiki.com/yussypu/polymarket-trading-infrastructure/5.4-error-handling-and-recovery) -- circuit breaker patterns, kill switch design, reconnection logic
- [Kalshi API Rate Limits](https://docs.kalshi.com/getting_started/rate_limits) -- official rate limit documentation
- [Kalshi Demo Environment](https://docs.kalshi.com/getting_started/demo_env) -- sandbox testing documentation
- [Polymarket py-clob-client](https://github.com/Polymarket/py-clob-client) -- official Python SDK for order execution
- [Kill Switch Design That Saved Us](https://medium.com/@fahimulhaq/the-kill-switch-design-that-saved-us-from-going-under-03bd140c749c) -- real-world kill switch implementation patterns
- [Algorithmic Trading Strategy Checklist](http://adventuresofgreg.com/blog/2025/12/15/algorithmic-trading-strategy-checklist-key-elements/) -- 12 key elements for production algo trading
- [Algorithmic Risk Controls (QuestDB)](https://questdb.com/glossary/algorithmic-risk-controls/) -- pre-trade risk management patterns
- [Cross-Platform Arbitrage Risk (CryptoDigger)](https://cryptodiffer.com/feed/project-updates/cross-platform-prediction-market-arbitrage-how-it-actually-works) -- settlement mismatch risks
- [Measuring Stale Data in Trading Systems](https://dataintellect.com/blog/stale-data-measuring-what-isnt-there/) -- staleness detection approaches
- [Production Kalshi Trading Bot (GitHub)](https://github.com/danielsilvaperez/trading-core) -- reference implementation with circuit breakers
- [Automated Trading on Polymarket (QuantVPS)](https://www.quantvps.com/blog/automated-trading-polymarket) -- execution strategies and risk controls
- [AI Trading Bot Risk Management Guide 2025 (3Commas)](https://3commas.io/blog/ai-trading-bot-risk-management-guide-2025) -- comprehensive risk management checklist

---
*Feature research for: cross-platform prediction market arbitrage*
*Researched: 2026-04-16*
