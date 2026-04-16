# Stack Research

**Domain:** Production prediction market arbitrage (live trading across Kalshi, Polymarket, PredictIt)
**Researched:** 2026-04-16
**Confidence:** HIGH (verified against official docs, PyPI, and platform changelogs)

## Critical Context: What Changed Since the Codebase Was Built

Three breaking developments affect the existing stack:

1. **Kalshi fixed-point migration (March 12, 2026):** Legacy integer cent fields removed. Prices now use `_dollars` strings with 4 decimal places (e.g., `"0.6500"`). The `market` order type is removed -- only `limit` orders are accepted. The `Pending` status enum value is removed. This is a **breaking change** for the existing collector and executor.

2. **Polymarket US launch (November 2025):** Polymarket now has a CFTC-regulated US platform at `polymarket.us` with a separate SDK (`polymarket-us`), Ed25519 auth, and different API endpoints. The international `py-clob-client` still works but only for the **international** platform. US-based users should use the US platform. This is a **critical decision point** for the existing Polymarket integration.

3. **PredictIt has NO trading API:** PredictIt only offers a read-only market data endpoint. There is no programmatic order placement. All trades must go through the web interface. This means PredictIt **cannot participate in automated arbitrage execution**. It can only serve as a price signal source for manual arbitrage or cross-platform spread monitoring.

## Recommended Stack

### Core Technologies (Keep / Upgrade)

| Technology | Current | Target | Purpose | Why Recommended | Confidence |
|------------|---------|--------|---------|-----------------|------------|
| Python | 3.12 | 3.12 | Backend runtime | Already in use. Async-first with asyncio. All platform SDKs are Python-native. No reason to change. | HIGH |
| aiohttp | >=3.9.0 | 3.13.5 | HTTP server + WS | Already in use. Native async, built-in WebSocket server/client. Sentry integration auto-detects it. Pin to 3.13.x for WS compression safety fixes. | HIGH |
| asyncpg | >=0.29.0 | 0.31.0 | PostgreSQL async driver | Already in use. Fastest Python PG driver. v0.31.0 adds Python 3.14 wheels and PG 18 support. | HIGH |
| redis[hiredis] | >=5.0.0 | 5.x+ | Quote cache + pub/sub | Already in use. hiredis C parser provides 10x faster RESP parsing. Keep current approach. | HIGH |
| PostgreSQL | 16 | 16 | Trade persistence | Already in use via Docker. No reason to upgrade -- PG16 is current LTS. | HIGH |
| Redis | 7 | 7 | In-memory cache | Already in use via Docker. Redis 7 has all needed features. | HIGH |

### Platform SDKs (Critical Changes Needed)

| Library | Current | Target | Purpose | Why Recommended | Confidence |
|---------|---------|--------|---------|-----------------|------------|
| py-clob-client | >=0.25.0 | 0.34.6 | Polymarket CLOB (international) | Official Polymarket SDK. v0.34.6 (Feb 2026) fixes tick size cache. **Only works with international Polymarket, NOT Polymarket US.** 1.1M monthly downloads; production-proven. | HIGH |
| polymarket-us | -- (new) | 0.1.2 | Polymarket US CLOB (CFTC-regulated) | Official Polymarket US SDK. Requires Python 3.10+. Uses Ed25519 auth (not Ethereum wallet signing). **Required if operating from a US account.** Very new (Jan 2026); fewer than 2 releases. | MEDIUM |
| kalshi-python | -- (not used) | 2.1.4 | Kalshi REST API | Official Kalshi SDK. Auto-generates from OpenAPI spec. Handles RSA-PSS auth. Released Sep 2025. Alternative: build raw signing with cryptography (current approach). | MEDIUM |
| kalshi-py | -- (not used) | 2.0.6.6 | Kalshi REST + async | Community SDK with built-in async support, RSA-PSS auth from env vars. Uses httpx. Released Aug 2025. Good DX but not official. | MEDIUM |
| cryptography | >=41.0.0 | 46.0.7 | Kalshi RSA-PSS signing | Already in use for Kalshi auth. v46.0.7 (Apr 2026) is latest stable. Keep this -- it's the standard for RSA-PSS in Python. No need for a Kalshi SDK if signing is already implemented. | HIGH |
| web3 | >=6.0.0 | 7.15.0 | Ethereum/Polygon interaction | Required by py-clob-client for Polymarket wallet signing. v7.15.0 (Apr 2026) is latest. **Major version bump from 6.x to 7.x -- check for breaking changes in your integration.** Not needed if switching to polymarket-us (Ed25519-based). | MEDIUM |

### New Libraries for Production Trading

| Library | Version | Purpose | Why Recommended | Confidence |
|---------|---------|---------|-----------------|------------|
| tenacity | 9.1.4 | Retry with backoff | Gold standard for Python retry logic. Async-native. Exponential backoff, jitter, circuit breaker compose. Essential for API call resilience. | HIGH |
| structlog | 25.5.0 | Structured logging | JSON-structured logs for production debugging. Async-safe. 10+ years in production use. Replace stdlib logging for trade execution paths. | HIGH |
| sentry-sdk[aiohttp] | 2.58.0 | Error tracking + alerting | Auto-detects aiohttp. Captures unhandled exceptions, slow transactions, structured logs. Free tier (5K errors/month) sufficient for this scale. | HIGH |
| websockets | 16.0 | WebSocket client connections | For Kalshi and Polymarket market data streaming. Pure Python, asyncio-native. Handles keepalive automatically. Requires Python 3.10+. | HIGH |
| pydantic | 2.x | Data validation | Validate order parameters, API responses, config. Prevents sending malformed orders to production APIs. Fast (Rust core). | MEDIUM |

### Development & Testing Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| pytest + pytest-asyncio | Python test runner | Already in use. Add pytest-asyncio for async test support. |
| respx | HTTP mocking for httpx | If switching Kalshi to kalshi-py (which uses httpx). Otherwise use aioresponses. |
| aioresponses | HTTP mocking for aiohttp | Mock platform API calls in tests without hitting real endpoints. |
| mypy | Static type checking | Catch type errors in order construction before runtime. Critical for financial code. |

## Installation

```bash
# Core (update existing requirements.txt)
pip install \
  aiohttp==3.13.5 \
  asyncpg==0.31.0 \
  "redis[hiredis]>=5.0.0" \
  cryptography==46.0.7 \
  python-dotenv>=1.0.0

# Platform SDKs (choose based on Polymarket platform decision)
# Option A: International Polymarket (current approach)
pip install py-clob-client==0.34.6 web3==7.15.0

# Option B: Polymarket US (CFTC-regulated, US accounts)
pip install polymarket-us==0.1.2
# Note: polymarket-us does NOT require web3 -- uses Ed25519 instead

# Kalshi (keep current custom signing OR use official SDK)
# Current approach: cryptography handles RSA-PSS signing directly
# Alternative: pip install kalshi-python==2.1.4

# Production resilience
pip install tenacity==9.1.4 structlog==25.5.0 "sentry-sdk[aiohttp]==2.58.0"

# WebSocket streaming
pip install websockets==16.0

# Dev dependencies
pip install pytest pytest-asyncio aioresponses mypy
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| py-clob-client 0.34.6 | polymarket-us 0.1.2 | **Use polymarket-us if** the operator's Polymarket account is on the US platform (polymarket.us). The two SDKs are incompatible -- different auth, different endpoints, different signing. |
| Custom Kalshi RSA signing (cryptography) | kalshi-python 2.1.4 (official SDK) | **Use kalshi-python if** starting fresh. But the existing codebase already has working RSA-PSS signing. Switching adds risk for no gain. Keep custom signing. |
| Custom Kalshi RSA signing (cryptography) | kalshi-py 2.0.6.6 (community, async) | **Use kalshi-py if** you want built-in async support and auto-generated typed methods. Has sync + async modes. But adds a dependency that duplicates existing functionality. |
| aiohttp (server + HTTP client) | httpx (HTTP client only) | **Use httpx if** you need HTTP/2 or want a cleaner client API. But aiohttp already handles both server and client needs. Adding httpx is unnecessary complexity. |
| structlog | loguru | **Use loguru if** you prefer simpler API. But structlog's JSON output and processor pipeline are better for production log aggregation. |
| tenacity | stamina | **Use stamina if** you want a simpler retry API with fewer options. But tenacity's circuit breaker composition and async support are more mature. |
| websockets 16.0 | aiohttp WebSocket client | **Use aiohttp WS client if** you want fewer dependencies. aiohttp has a built-in WS client that may be sufficient. websockets library is more robust for long-lived connections with automatic keepalive. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| kalshi-python-async 3.4.0 | Unofficial fork, unclear maintenance. Version numbering suggests independent development. | kalshi-python 2.1.4 (official) or custom signing with cryptography |
| kalshi-python-unofficial | Lightweight wrapper but no guarantee of API coverage or timely updates. | cryptography (direct signing) or kalshi-python (official) |
| py-clob-client for Polymarket US | py-clob-client uses Ethereum wallet signing. Polymarket US uses Ed25519. Incompatible. | polymarket-us 0.1.2 for US platform |
| web3 6.x | Major version behind. py-clob-client 0.34.6 may require web3 7.x features. Check compatibility. | web3 7.15.0 |
| PredictIt for automated trading | No trading API exists. Read-only market data only. Building web scraping/automation violates ToS and is fragile. | Use PredictIt for price signals only. Execute arbitrage on Kalshi + Polymarket. |
| print() for trade logging | No structure, no levels, no aggregation. Impossible to debug production issues. | structlog with JSON output |
| Manual retry loops | Inconsistent backoff, no jitter, no circuit breaking. Will hit rate limits. | tenacity with exponential backoff + jitter |

## Stack Patterns by Variant

**If using international Polymarket (non-US account):**
- Keep py-clob-client 0.34.6 + web3 7.15.0
- Auth: Ethereum private key signing
- Endpoint: clob.polymarket.com
- Collateral: USDC.e on Polygon
- Because: Existing integration is closest to working. Most mature SDK.

**If using Polymarket US (US account, CFTC-regulated):**
- Switch to polymarket-us 0.1.2
- Auth: Ed25519 key pair (generated at polymarket.us/developer)
- Endpoint: api.polymarket.us
- Collateral: Polymarket USD (PMUSD) -- new as of April 6, 2026
- **WARNING:** polymarket-us is very new (2 releases, Jan 2026). Expect rough edges.
- Because: Legal requirement for US-based trading. Different auth model entirely.

**If Kalshi auth is already working:**
- Keep custom RSA-PSS signing with cryptography 46.0.7
- Do NOT switch to kalshi-python SDK -- migration risk with no functional gain
- Because: Working auth code is more valuable than a prettier API wrapper.

**If Kalshi auth needs to be rewritten (e.g., broken by fixed-point migration):**
- Consider kalshi-python 2.1.4 (official SDK) as replacement
- Handles auth, field format changes, and endpoint routing
- Because: If you're rewriting anyway, use the official SDK to get free maintenance.

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| py-clob-client 0.34.6 | Python >=3.9.10, web3 (check pinned version in py-clob-client's requirements) | web3 version is pinned internally by py-clob-client. Do NOT override. |
| polymarket-us 0.1.2 | Python >=3.10 | Does NOT depend on web3. Uses Ed25519 signing. |
| kalshi-python 2.1.4 | Python >=3.9 | Auto-generated from OpenAPI. May lag behind API changes. |
| websockets 16.0 | Python >=3.10 | Requires Python 3.10+. Current Docker image uses 3.12, so compatible. |
| aiohttp 3.13.5 | Python >=3.9 | Check for breaking changes from 3.9.x. WebSocket compression fix is important. |
| web3 7.15.0 | Python >=3.8 (check) | Major version bump from 6.x. Middleware API changed. Test Polymarket signing carefully. |
| structlog 25.5.0 | Python >=3.8 | Integrates with stdlib logging. Can adopt incrementally. |
| sentry-sdk 2.58.0 | aiohttp 3.x auto-detected | Just pip install and call sentry_sdk.init(). |

## Platform API Rate Limits (Must Encode in System)

| Platform | Read Limit | Write Limit | Notes |
|----------|-----------|-------------|-------|
| Kalshi (Basic tier) | 20/sec | 10/sec (orders) | Batch items count individually. Apply for Advanced tier (free) for 30/sec write. |
| Kalshi (Advanced tier) | 30/sec | 30/sec | Free upgrade on request. Recommended for production. |
| Polymarket (CLOB) | 60/min (REST) | 3,500/10s burst, 60/s sustained (orders) | WebSocket connections don't count against REST limits. Use WS for price data. |
| PredictIt | 1/sec | N/A (no trading API) | Read-only. 1 request per second. |

## Platform Sandbox/Testing Environments

| Platform | Sandbox Available | Details |
|----------|-------------------|---------|
| Kalshi | YES -- demo-api.kalshi.co | Full sandbox with fake money. Separate API keys. WS at wss://demo-api.kalshi.co/trade-api/v2/ws. **Test here first.** |
| Polymarket (international) | NO | No testnet. Test with small real amounts on mainnet. Use min order sizes. |
| Polymarket US | NO (likely) | Very new platform. No sandbox documented. |
| PredictIt | N/A | No trading API to test. |

## Key Decision: Polymarket SDK Choice

This is the single most important stack decision. The operator must determine:

1. **Is the Polymarket account on polymarket.com (international) or polymarket.us (US)?**
2. If international: keep py-clob-client, upgrade to 0.34.6, upgrade web3 to 7.15.0
3. If US: replace entire Polymarket integration with polymarket-us 0.1.2

This decision cascades into auth method, collateral type, API endpoints, and WebSocket URLs. It cannot be deferred.

## Sources

- [Kalshi API Changelog](https://docs.kalshi.com/changelog) -- Verified fixed-point migration, order type removal, status enum changes (HIGH confidence)
- [Kalshi Rate Limits](https://docs.kalshi.com/getting_started/rate_limits) -- Tier-based limits verified from official docs (HIGH confidence)
- [Kalshi Demo Environment](https://docs.kalshi.com/getting_started/demo_env) -- Sandbox URL and features confirmed (HIGH confidence)
- [py-clob-client PyPI](https://pypi.org/project/py-clob-client/) -- Version 0.34.6, Feb 2026 (HIGH confidence)
- [polymarket-us PyPI](https://libraries.io/pypi/polymarket-us) -- Version 0.1.2, Jan 2026 (HIGH confidence)
- [Polymarket US Python SDK GitHub](https://github.com/Polymarket/polymarket-us-python) -- Official repo, Python 3.10+ (HIGH confidence)
- [Polymarket CFTC Approval](https://www.coindesk.com/business/2025/11/25/polymarket-secures-cftc-approval-for-regulated-u-s-return/) -- US platform launch confirmed (HIGH confidence)
- [Polymarket Rate Limits Guide](https://agentbets.ai/guides/polymarket-rate-limits-guide/) -- Burst/sustained limits (MEDIUM confidence, third-party source)
- [kalshi-python PyPI](https://pypi.org/project/kalshi-python/) -- Version 2.1.4, Sep 2025 (HIGH confidence)
- [kalshi-py PyPI](https://pypi.org/project/kalshi-py/) -- Version 2.0.6.6, Aug 2025 (HIGH confidence)
- [tenacity PyPI](https://pypi.org/project/tenacity/) -- Version 9.1.4, Feb 2026 (HIGH confidence)
- [structlog PyPI](https://pypi.org/project/structlog/) -- Version 25.5.0, Oct 2025 (HIGH confidence)
- [sentry-sdk PyPI](https://pypi.org/project/sentry-sdk/) -- Version 2.58.0 with aiohttp integration (HIGH confidence)
- [websockets PyPI](https://pypi.org/project/websockets/) -- Version 16.0, Python 3.10+ (HIGH confidence)
- [aiohttp PyPI](https://pypi.org/project/aiohttp/) -- Version 3.13.5, Mar 2026 (HIGH confidence)
- [asyncpg PyPI](https://pypi.org/project/asyncpg/) -- Version 0.31.0, Nov 2025 (HIGH confidence)
- [cryptography PyPI](https://pypi.org/project/cryptography/) -- Version 46.0.7, Apr 2026 (HIGH confidence)
- [web3 PyPI](https://pypi.org/project/web3/) -- Version 7.15.0, Apr 2026 (HIGH confidence)
- [PredictIt API limitation](https://newyorkcityservers.com/blog/best-prediction-market-apis) -- Read-only confirmed from multiple sources (HIGH confidence)
- [Polymarket WebSocket docs](https://agentbets.ai/guides/polymarket-websocket-guide/) -- Channel types, heartbeat API (MEDIUM confidence)
- [Kalshi WebSocket docs](https://docs.kalshi.com/getting_started/quick_start_websockets) -- Channel types, auth headers (HIGH confidence)

---
*Stack research for: Production prediction market arbitrage (live trading)*
*Researched: 2026-04-16*
