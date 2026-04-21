# Polymarket US Pivot + Scale-to-Thousands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working `api.polymarket.us` integration with Ed25519 auth, rewrite the scanner to event-driven O(1)-per-match, add a 3-layer resolution-equivalence gate, and auto-discover thousands of market pairs — while keeping every existing safety invariant intact and all 407 legacy tests green.

**Architecture:** Two Polymarket variants (`legacy` CLOB + new `us` DCM) selected by `POLYMARKET_VARIANT` env flag; new collector + adapter parallel to the legacy ones; scanner rewritten around a bounded `asyncio.Queue` matcher with per-canonical debounce and backpressure; auto-discovery pipeline gated on a 3-layer resolution check (structured fields, LLM verifier, fixture corpus).

**Tech Stack:** Python 3.12, `polymarket-us` PyPI SDK (with hand-rolled Ed25519 fallback via `cryptography`), aiohttp, asyncpg, asyncio, pytest + aioresponses, Anthropic SDK for LLM verifier (Haiku 4.5), Playwright MCP for onboarding.

**Source spec:** `docs/superpowers/specs/2026-04-21-polymarket-us-pivot-and-scale-design.md`

---

## Waves (parallelizable groupings)

| Wave | Tasks | Depends on | Parallel? |
|---|---|---|---|
| 1 — Foundation | 1 signer · 2 fee · 3 config/variant · 4 env template | none | yes |
| 2 — Collector + Adapter | 5 REST · 6 WS · 7 adapter · 8 hard-lock tests | Wave 1 | yes within wave |
| 3 — Scanner | 9 event matcher · 10 backpressure · 11 scale test | Wave 2 | sequential (same file) |
| 4 — Mapping | 12 resolution-check · 13 LLM verifier · 14 auto-discovery · 15 auto-promote gate | Wave 1 | yes within wave |
| 5 — Ops | 16 preflight split · 17 check_polymarket_us.py · 18 metrics + heartbeat | Waves 2+4 | yes within wave |
| 6 — Onboarding & Finalize | 19 Playwright onboarding · 20 full-suite · 21 Step-5 Telegram hand-off | Waves 1–5 | sequential |

---

## Task 1 — Ed25519 signer module

**Files:**
- Create: `arbiter/auth/ed25519_signer.py`
- Create: `arbiter/auth/__init__.py` (empty if new dir)
- Test: `arbiter/auth/test_ed25519_signer.py`

- [ ] **Step 1: Write failing tests**

```python
# arbiter/auth/test_ed25519_signer.py
import base64
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from arbiter.auth.ed25519_signer import Ed25519Signer, SignatureError

KEYPAIR_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # 32 zero-to-31 bytes
KEY_ID = "test-key-id"

def test_headers_shape():
    s = Ed25519Signer(key_id=KEY_ID, secret_b64=KEYPAIR_B64)
    h = s.headers("GET", "/v1/markets", ts_ms=1000000000000)
    assert h["X-PM-Access-Key"] == KEY_ID
    assert h["X-PM-Timestamp"] == "1000000000000"
    assert "X-PM-Signature" in h

def test_signature_payload_excludes_body():
    s = Ed25519Signer(key_id=KEY_ID, secret_b64=KEYPAIR_B64)
    h1 = s.headers("POST", "/v1/orders", ts_ms=42, body='{"price":"0.51"}')
    h2 = s.headers("POST", "/v1/orders", ts_ms=42, body='{"price":"0.99"}')
    assert h1["X-PM-Signature"] == h2["X-PM-Signature"], \
        "Signature must NOT depend on body (spec §4 correction)"

def test_wrong_secret_length_raises():
    with pytest.raises(SignatureError, match="32 bytes"):
        Ed25519Signer(key_id="k", secret_b64=base64.b64encode(b"short").decode())

def test_signature_deterministic_and_verifiable():
    s = Ed25519Signer(key_id=KEY_ID, secret_b64=KEYPAIR_B64)
    h = s.headers("GET", "/v1/markets", ts_ms=1000)
    sig_bytes = base64.b64decode(h["X-PM-Signature"])
    pub = Ed25519PrivateKey.from_private_bytes(base64.b64decode(KEYPAIR_B64)[:32]).public_key()
    pub.verify(sig_bytes, b"1000GET/v1/markets")  # raises if bad
```

- [ ] **Step 2: Run tests, confirm they fail** — `pytest arbiter/auth/test_ed25519_signer.py -v` → all 4 fail with ImportError.

- [ ] **Step 3: Implement**

```python
# arbiter/auth/ed25519_signer.py
import base64
import time
from dataclasses import dataclass
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

class SignatureError(ValueError):
    pass

@dataclass
class Ed25519Signer:
    key_id: str
    secret_b64: str

    def __post_init__(self) -> None:
        raw = base64.b64decode(self.secret_b64)
        if len(raw) < 32:
            raise SignatureError(f"secret must be >= 32 bytes, got {len(raw)}")
        self._pk = Ed25519PrivateKey.from_private_bytes(raw[:32])

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)

    def headers(self, method: str, path: str, ts_ms: int | None = None, body: str | None = None) -> dict[str, str]:
        # Body intentionally excluded — docs.polymarket.us/api-reference/authentication
        ts = ts_ms if ts_ms is not None else self.now_ms()
        msg = f"{ts}{method.upper()}{path}".encode()
        sig = self._pk.sign(msg)
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": str(ts),
            "X-PM-Signature": base64.b64encode(sig).decode(),
        }
```

- [ ] **Step 4: Run tests, confirm pass** — `pytest arbiter/auth/test_ed25519_signer.py -v` → 4 pass.

- [ ] **Step 5: Commit** — `git add arbiter/auth/ && git commit -m "feat(auth): Ed25519 signer for Polymarket US (payload excludes body)"`

---

## Task 2 — Polymarket US fee function

**Files:**
- Modify: `arbiter/config/settings.py` (add new fn, leave old untouched)
- Test: `arbiter/config/test_polymarket_us_fee.py`

- [ ] **Step 1: Write failing tests**

```python
# arbiter/config/test_polymarket_us_fee.py
import math
from arbiter.config.settings import polymarket_us_order_fee

def test_taker_fee_at_fifty_cents():
    # Θ_taker * C * p * (1-p) = 0.05 * 100 * 0.5 * 0.5 = 1.25
    fee = polymarket_us_order_fee(price=0.50, qty=100, intent="taker")
    assert math.isclose(fee, 1.25, abs_tol=0.005)

def test_maker_fee_is_negative_rebate():
    # Θ_maker = -0.0125 → negative = rebate
    fee = polymarket_us_order_fee(price=0.50, qty=100, intent="maker")
    assert fee < 0, "maker must return negative (rebate)"
    assert math.isclose(fee, -0.3125, abs_tol=0.005)

def test_symmetric_in_price():
    # f(0.3) == f(0.7)
    a = polymarket_us_order_fee(price=0.3, qty=100, intent="taker")
    b = polymarket_us_order_fee(price=0.7, qty=100, intent="taker")
    assert math.isclose(a, b, abs_tol=0.005)

def test_rounds_to_cent_bankers():
    fee = polymarket_us_order_fee(price=0.51, qty=5, intent="taker")
    cents = round(fee * 100)
    assert fee * 100 == cents  # exact cent

def test_zero_fee_at_edges():
    assert polymarket_us_order_fee(price=0.0, qty=100, intent="taker") == 0.0
    assert polymarket_us_order_fee(price=1.0, qty=100, intent="taker") == 0.0
```

- [ ] **Step 2: Run → fail (ImportError).**

- [ ] **Step 3: Implement** — append to `arbiter/config/settings.py`:

```python
# Polymarket US fee curve — docs.polymarket.us/fees
_THETA_TAKER = 0.05
_THETA_MAKER = -0.0125  # rebate, signed negative

def polymarket_us_order_fee(price: float, qty: float, intent: str = "taker") -> float:
    theta = _THETA_TAKER if intent == "taker" else _THETA_MAKER
    raw = theta * qty * price * (1.0 - price)
    # Banker's rounding to cent
    return round(raw * 100) / 100.0
```

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit** — `git add arbiter/config/ && git commit -m "feat(fees): polymarket_us_order_fee with signed maker rebate"`

---

## Task 3 — POLYMARKET_VARIANT flag + config class split

**Files:**
- Modify: `arbiter/config/settings.py` (add PolymarketUSConfig class, add VARIANT selector)
- Test: `arbiter/config/test_variant_selection.py`

- [ ] **Step 1: Write failing tests**

```python
# arbiter/config/test_variant_selection.py
import os
from arbiter.config.settings import load_config, PolymarketConfig, PolymarketUSConfig

def test_variant_defaults_to_us(monkeypatch):
    monkeypatch.delenv("POLYMARKET_VARIANT", raising=False)
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketUSConfig)

def test_variant_legacy_returns_legacy_class(monkeypatch):
    monkeypatch.setenv("POLYMARKET_VARIANT", "legacy")
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "0"*64)
    monkeypatch.setenv("POLY_FUNDER", "0x" + "1"*40)
    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketConfig)

def test_variant_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")
    cfg = load_config()
    assert cfg.polymarket is None
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** — add to `settings.py`:

```python
@dataclass
class PolymarketUSConfig:
    api_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1"))
    ws_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_WS_URL", "wss://api.polymarket.us/v1/ws/markets"))
    api_key_id: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_API_KEY_ID", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_API_SECRET", ""))
    poll_interval: float = 1.0
    ws_enabled: bool = True
```

Modify `load_config()`:

```python
variant = os.getenv("POLYMARKET_VARIANT", "us").lower()
if variant == "disabled":
    polymarket_cfg = None
elif variant == "legacy":
    polymarket_cfg = PolymarketConfig()
else:  # "us" (default)
    polymarket_cfg = PolymarketUSConfig()
```

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat(config): POLYMARKET_VARIANT flag + PolymarketUSConfig class"`

---

## Task 4 — `.env.production.template` update

**Files:**
- Modify: `.env.production.template`

- [ ] **Step 1: Read current template** — lines 67-88 (Polymarket section).

- [ ] **Step 2: Add new US-variant section above the legacy section; mark legacy section "legacy, optional."**

```
# ---- Polymarket (US — default, required for live trading) ----
POLYMARKET_VARIANT=us
POLYMARKET_US_API_URL=https://api.polymarket.us/v1
POLYMARKET_US_WS_URL=wss://api.polymarket.us/v1/ws/markets
POLYMARKET_US_API_KEY_ID=<paste Key ID from polymarket.us/developer>
POLYMARKET_US_API_SECRET=<paste base64 Ed25519 secret, shown once on key creation>

# ---- Polymarket (legacy non-US CLOB, optional; set POLYMARKET_VARIANT=legacy to use) ----
# POLYMARKET_CLOB_URL=https://clob.polymarket.com
# POLY_PRIVATE_KEY=<64-hex Ethereum private key>
# POLY_FUNDER=<0x address>
# POLY_SIGNATURE_TYPE=2
# POLYGON_RPC_URL=https://polygon-rpc.com
```

- [ ] **Step 3: Commit** — `git add .env.production.template && git commit -m "docs(env): Polymarket US variant default in production template"`

---

## Task 5 — Polymarket US REST client

**Files:**
- Create: `arbiter/collectors/polymarket_us.py`
- Create: `arbiter/collectors/test_polymarket_us.py`

- [ ] **Step 1: Write failing tests with `aioresponses`**

```python
# arbiter/collectors/test_polymarket_us.py
import pytest
from aioresponses import aioresponses
from arbiter.collectors.polymarket_us import PolymarketUSClient
from arbiter.auth.ed25519_signer import Ed25519Signer

SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

@pytest.fixture
def client():
    signer = Ed25519Signer(key_id="kid", secret_b64=SECRET)
    return PolymarketUSClient(base_url="https://api.polymarket.us/v1", signer=signer)

@pytest.mark.asyncio
async def test_list_markets_paginates(client):
    with aioresponses() as m:
        m.get("https://api.polymarket.us/v1/markets?limit=100&offset=0",
              payload={"markets": [{"slug": "m1"}], "hasMore": True})
        m.get("https://api.polymarket.us/v1/markets?limit=100&offset=100",
              payload={"markets": [{"slug": "m2"}], "hasMore": False})
        results = [m async for m in client.list_markets()]
        slugs = [r["slug"] for r in results]
        assert slugs == ["m1", "m2"]

@pytest.mark.asyncio
async def test_get_orderbook_returns_bids_offers(client):
    with aioresponses() as m:
        m.get("https://api.polymarket.us/v1/orderbook/foo?depth=3",
              payload={"bids":[{"px":50,"qty":100}],"offers":[{"px":55,"qty":50}]})
        ob = await client.get_orderbook("foo", depth=3)
        assert ob["bids"][0]["px"] == 50

@pytest.mark.asyncio
async def test_place_fok_order_sends_signed_post(client):
    with aioresponses() as m:
        m.post("https://api.polymarket.us/v1/orders",
               payload={"orderId": "ord-123", "status": "FILLED"})
        r = await client.place_order(slug="foo", intent="BUY_LONG",
                                     price=0.51, qty=100, tif="FILL_OR_KILL")
        assert r["orderId"] == "ord-123"

@pytest.mark.asyncio
async def test_rate_limit_retry_on_429(client):
    with aioresponses() as m:
        m.get("https://api.polymarket.us/v1/account/balances",
              status=429, headers={"Retry-After":"0"})
        m.get("https://api.polymarket.us/v1/account/balances",
              payload={"currentBalance": "100.00"})
        bal = await client.balance()
        assert bal["currentBalance"] == "100.00"
```

- [ ] **Step 2: Run → fail (ImportError).**

- [ ] **Step 3: Implement** — `arbiter/collectors/polymarket_us.py`:

```python
import asyncio
import aiohttp
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from arbiter.auth.ed25519_signer import Ed25519Signer

@dataclass
class PolymarketUSClient:
    base_url: str
    signer: Ed25519Signer
    session: Optional[aiohttp.ClientSession] = None
    rate_limit_rps: int = 20

    async def _ensure(self) -> aiohttp.ClientSession:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _signed(self, method: str, path: str, json_body: dict | None = None) -> dict:
        sess = await self._ensure()
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            headers = self.signer.headers(method, path)
            async with sess.request(method, url, headers=headers, json=json_body) as r:
                if r.status == 429:
                    await asyncio.sleep(float(r.headers.get("Retry-After", "0")))
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError("rate-limit retry exhausted")

    async def list_markets(self, page_size: int = 100) -> AsyncIterator[dict]:
        offset = 0
        while True:
            path = f"/markets?limit={page_size}&offset={offset}"
            data = await self._signed("GET", path)
            for m in data.get("markets", []):
                yield m
            if not data.get("hasMore"):
                break
            offset += page_size

    async def get_orderbook(self, symbol: str, depth: int = 10) -> dict:
        return await self._signed("GET", f"/orderbook/{symbol}?depth={depth}")

    async def place_order(self, slug: str, intent: str, price: float, qty: int, tif: str = "FILL_OR_KILL") -> dict:
        body = {
            "marketSlug": slug,
            "intent": f"ORDER_INTENT_{intent}",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": str(price), "currency": "USD"},
            "quantity": qty,
            "tif": f"TIF_{tif}",
        }
        return await self._signed("POST", "/orders", json_body=body)

    async def cancel_order(self, order_id: str, slug: str) -> dict:
        return await self._signed("POST", f"/order/{order_id}/cancel", json_body={"marketSlug": slug})

    async def balance(self) -> dict:
        return await self._signed("GET", "/account/balances")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
```

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat(collectors): Polymarket US REST client (signed, paginated, 429-retry)"`

---

## Task 6 — Polymarket US WebSocket multiplex

**Files:**
- Create: `arbiter/collectors/polymarket_us_ws.py`
- Test: `arbiter/collectors/test_polymarket_us_ws.py`

- [ ] **Step 1: Write failing tests** — mock WS with `aiohttp.ClientWebSocketResponse` stub; verify: (a) chunks slugs into ≤100-per-connection subs, (b) reconnects on unclean close, (c) emits updates through a shared async queue.

- [ ] **Step 2: Implement** — `PolymarketUSWebSocket` class: opens N connections where N=ceil(len(slugs)/100), sends `{"subscribe":{"requestId":"...","subscriptionType":"SUBSCRIPTION_TYPE_MARKET_DATA","marketSlugs":[...]}}`, merges streams.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 7 — PolymarketUSAdapter (execution)

**Files:**
- Create: `arbiter/execution/adapters/polymarket_us.py`
- Test: `arbiter/execution/adapters/test_polymarket_us_adapter.py`

- [ ] **Step 1: Write failing tests for**: FOK happy path, PHASE4 hard-lock trip, PHASE5 hard-lock trip, supervisor-armed rejection, signing error propagation, order-id threading.

- [ ] **Step 2: Implement** following the §5.2 pseudocode from the spec — hard-locks run BEFORE `_sign_and_prepare`. Hooks into the existing `ExecutionEngine` via the `PlatformAdapter` protocol.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 8 — Hard-lock integration tests (Phase5 on US path)

**Files:**
- Create: `arbiter/execution/adapters/test_phase5_hardlock_us.py`

- [ ] **Step 1:** Port the PHASE5 hard-lock tests from `test_phase5_hardlock.py` (legacy) to the US adapter. Every test in the legacy file must have a US counterpart with identical semantics.

- [ ] **Step 2: All green.**

- [ ] **Step 3: Commit.**

---

## Task 9 — Event-driven matcher (scanner rewrite, part 1)

**Files:**
- Create: `arbiter/scanner/matched_pair_stream.py`
- Modify: `arbiter/scanner/arbitrage.py` (plug matcher in; keep legacy `scan_once` alive for tests)
- Test: `arbiter/scanner/test_matched_pair_stream.py`

- [ ] **Step 1: Tests first** — property-style: for any sequence of quote updates across 1000 canonical pairs, `MatchedPairStream` yields exactly one pair per (canonical, both-sides-present) and never yields a pair with only one side.

- [ ] **Step 2: Implement** — O(1) per quote event: `on_quote(platform, canonical_id, quote)` updates `_store[canonical_id][platform]`, and if both platforms present, emits `MatchedPair(canonical_id, kalshi_quote, poly_quote)` to the output queue.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 10 — Backpressure + debounce + emit throttle (scanner part 2)

**Files:**
- Modify: `arbiter/scanner/matched_pair_stream.py`
- Test: `arbiter/scanner/test_scanner_backpressure.py`

- [ ] **Step 1: Tests** — (a) `asyncio.Queue(maxsize=5000)` overflow drops oldest + increments counter, (b) per-canonical debounce coalesces > 20 updates/sec to 1 match/50ms, (c) opportunity emit limited to 10/sec per (canonical, side).

- [ ] **Step 2: Implement** — add bounded queue, debounce timer per canonical, token-bucket emit throttle.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 11 — Scale test at n=1000

**Files:**
- Create: `arbiter/scanner/test_scale_1000.py`

- [ ] **Step 1: Write test** that synthetically drives 1000 canonical pairs × 5 updates/sec for 60s, asserts p99 match-to-emit latency ≤ 100ms, asserts zero panics, asserts backpressure drops < 1% of events.

- [ ] **Step 2: Run, tune debounce/queue size until green.**

- [ ] **Step 3: Commit.**

---

## Task 12 — Resolution-check Layer 1 (structured fields)

**Files:**
- Create: `arbiter/mapping/resolution_check.py`
- Create: `arbiter/mapping/fixtures/known_equivalent_pairs.json`
- Create: `arbiter/mapping/fixtures/known_divergent_pairs.json`
- Test: `arbiter/mapping/test_resolution_check.py`

- [ ] **Step 1: Build fixture corpus** — 20+ equivalent pairs (from existing MARKET_MAP + obvious cross-platform twins), 20+ divergent pairs (same topic, different resolution date / rule / source).

- [ ] **Step 2: Tests** — every fixture classified correctly; unit tests for each divergence type (date, source, tie-break, category).

- [ ] **Step 3: Implement** — extract structured fields, run equivalence rules, return `ResolutionMatch` enum.

- [ ] **Step 4: Tests pass.**

- [ ] **Step 5: Commit.**

---

## Task 13 — Resolution-check Layer 2 (LLM verifier)

**Files:**
- Create: `arbiter/mapping/llm_verifier.py`
- Test: `arbiter/mapping/test_llm_verifier.py`

- [ ] **Step 1: Tests** — mock Anthropic SDK; verify prompt format, YES/NO/MAYBE parsing, response caching (same pair → no second API call), failure mode returns MAYBE (fail-safe).

- [ ] **Step 2: Implement** — minimal: `await verify(kalshi_q, poly_q) -> Literal["YES","NO","MAYBE"]`. Claude Haiku 4.5, prompt cache on system prompt, one-shot example. SDK: `anthropic` package with prompt caching.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 14 — Auto-discovery pipeline

**Files:**
- Create: `arbiter/mapping/auto_discovery.py`
- Test: `arbiter/mapping/test_auto_discovery.py`

- [ ] **Step 1: Tests** — given mock Kalshi `/markets` + Polymarket US `/v1/markets` responses, verify candidate pairs are scored and written to DB with status=candidate.

- [ ] **Step 2: Implement** — async pipeline: pull both platforms → cartesian product (capped) → score → write candidates. Rate-limit budget respects 2 r/s for discovery.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 15 — Auto-promote gate (8 conditions)

**Files:**
- Create: `arbiter/mapping/auto_promote.py`
- Test: `arbiter/mapping/test_auto_promote.py`

- [ ] **Step 1: Tests** — one negative-path test per condition (score_low, resolution_divergent, llm_no, liquidity_low, date_out_of_window, daily_cap, cooling_off_active, auto_promote_disabled). Plus one happy-path test.

- [ ] **Step 2: Implement** — `async def maybe_promote(mapping) -> PromotionResult` running all 8 conditions; increments `auto_promote_rejections_total{reason=...}` counter.

- [ ] **Step 3: Tests pass.**

- [ ] **Step 4: Commit.**

---

## Task 16 — Preflight split (5a credentials-only + 5b live)

**Files:**
- Modify: `arbiter/live/preflight.py` (around line 232-249)
- Test: `arbiter/live/test_preflight_polymarket_us.py`

- [ ] **Step 1: Tests** — 5a passes with just env vars set (no network); 5b passes with mocked balance ≥ $20; 5b fails with balance < $20; 5b skipped unless `PREFLIGHT_ALLOW_LIVE=1`.

- [ ] **Step 2: Implement** — `_check_05a_polymarket_us_credentials()` (presence + Ed25519 key shape), `_check_05b_polymarket_us_balance()` (signed GET /v1/account/balances).

- [ ] **Step 3: Update `go_live.sh`** to set `PREFLIGHT_ALLOW_LIVE=1` before invoking preflight.

- [ ] **Step 4: Commit.**

---

## Task 17 — `check_polymarket_us.py` setup script

**Files:**
- Create: `scripts/setup/check_polymarket_us.py`
- Modify: `scripts/setup/go_live.sh` (swap `check_polymarket.py` → `check_polymarket_us.py` when VARIANT=us)

- [ ] **Step 1:** Follow the shape of `check_kalshi_auth.py` — signed round-trip against `GET /v1/account/balances`, print presence+length+balance (no secret), exit 0/1.

- [ ] **Step 2: Run manually against stub/mocked env** — exit 0.

- [ ] **Step 3: Commit.**

---

## Task 18 — Prometheus metrics + Telegram heartbeat

**Files:**
- Modify: `arbiter/api.py` (metrics endpoint — add new counters/histograms)
- Create: `arbiter/notifiers/heartbeat.py`
- Test: `arbiter/notifiers/test_heartbeat.py`

- [ ] **Step 1: Tests** — heartbeat emits every 15min with realized_pnl + open_order_count; silent-mode when AUTO_EXECUTE_ENABLED=false.

- [ ] **Step 2: Implement** — background task spawned in `main.run_system`; reuses existing `TelegramNotifier`.

- [ ] **Step 3: Add metrics** from §7 of spec — all new counters/histograms registered and populated.

- [ ] **Step 4: Commit.**

---

## Task 19 — Playwright onboarding script

**Files:**
- Create: `scripts/setup/onboard_polymarket_us.py`

- [ ] **Step 1: Script** — opens browser to `https://polymarket.us/developer` via Playwright MCP, waits for operator login (heartbeat to Telegram), navigates to key-generation, captures secret from DOM field, writes to `.env.production` via Edit, closes page, deletes any screenshots.

- [ ] **Step 2: Dry-run against a local HTML fixture** that mimics the portal; verify no secret leaks to stdout/logs.

- [ ] **Step 3: Commit.**

---

## Task 20 — Full suite + preflight + tsc

- [ ] **Step 1: `pytest -q`** → 407 legacy + new ≥ 40 all pass.
- [ ] **Step 2: `npx tsc --noEmit`** → zero errors.
- [ ] **Step 3: Run preflight against stub env** → PREFLIGHT_ALLOW_LIVE=0, only 5a subchecks run, should PASS.
- [ ] **Step 4: Push to main.**

---

## Task 21 — Telegram deliverable (Step 5 hand-off)

- [ ] **Step 1:** Send two pastable commands to the operator via Telegram:

```
# Step 5 — first supervised live trade (run in terminal, kill-switch in browser):
docker compose -f docker-compose.prod.yml exec arbiter-api-prod \
    pytest -m live --live arbiter/live/test_first_live_trade.py -v -s

# Step 6 — after Step 5 clean, flip auto-execute:
sed -i '' 's/^AUTO_EXECUTE_ENABLED=.*/AUTO_EXECUTE_ENABLED=true/' .env.production
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod | grep auto_executor
```

- [ ] **Step 2: Stop.** Operator runs Step 5 and Step 6 themselves.

---

## Execution notes

- **DRY:** reuse existing `RateLimiter`, `CircuitBreaker`, `PriceStore`, `PlatformAdapter` protocol.
- **YAGNI:** no new abstractions unless a second consumer exists today.
- **TDD:** every task begins with failing tests; never write implementation without a failing test.
- **Frequent commits:** each task ends in a commit pushed to `main`.
- **Safety invariants:** kill-switch, hard-locks, SAFE-01..06 untouched unless test specifically proves they still hold.
- **Secrets hygiene:** `.env.production` and `keys/*.pem` stay gitignored; Ed25519 secret never echoed.

## Per-task regression gate (applies to every task)

**Before any task's final commit step, run `pytest -q` and confirm the full suite is still green.** If a task mutates a shared module (`settings.py`, `scanner/arbitrage.py`, `execution/engine.py`, `main.py`), this is non-negotiable. Broken-legacy discovered N tasks later costs hours of bisect. Tasks 2, 3, 9 specifically call this out but the rule is universal.

If a subagent reports "X tests now fail, will be fixed in Task N+1," STOP. A task is not complete until everything is green.

## Additions to specific tasks (from plan review round 1)

**Task 7 — hard-lock ORDER test (was C1):**

Add a test that monkeypatches `_sign_and_prepare` to raise, then calls `place_fok` with oversized notional. PHASE4 hard-lock must trip first (so `_sign_and_prepare` is never called). Assert:
- `_sign_and_prepare.call_count == 0`
- Raised exception message contains "PHASE4"

Add a parallel test with PHASE4 disabled but PHASE5 set — same assertion, message contains "PHASE5."

Add a test with supervisor.is_armed=True — same assertion.

**Task 15 — liquidity depth test (was M1):**

For condition #5 (liquidity ≥ `PHASE5_MAX_ORDER_USD × 2`), test must construct a fake order book with known depth and assert the gate returns correct pass/fail based on arithmetic, not a boolean fixture.

**Task 16 — 5b refuses to run without flag (was M3):**

Explicit test: `PREFLIGHT_ALLOW_LIVE` unset AND credentials present → 5b is skipped with exit code indicating skip-not-fail. Then set the flag → 5b actually runs. This guards the "5b never runs in CI" invariant.

**Task 17 — secret-leak test (was M4):**

`check_polymarket_us.py` test captures stdout+stderr, runs the script with a known fake secret in env, greps the output — MUST NOT contain the secret. Same for stderr.

**Task 11 — realistic rate (was M5):**

Drop synthetic rate to 3 updates/sec × 1000 markets = 3000 events/sec. Assert:
- p99 match-to-emit latency ≤ 100ms
- Backpressure drop rate < 0.1% (separate assertion, not bundled into latency)

## Task 19.5 — Rollback smoke test (was C3, NEW)

**Files:**
- Create: `arbiter/live/test_rollback_variants.py`

- [ ] **Step 1: Test `POLYMARKET_VARIANT=disabled`** — boot `main.run_system` for 5 seconds in a subprocess with variant=disabled; assert:
  - No HTTP calls to any Polymarket host (monkeypatch aiohttp at module level or use `aioresponses` assert-not-called)
  - Kalshi collector still runs
  - Scanner produces no opportunities (only one platform present)
  - Process exits cleanly on SIGTERM

- [ ] **Step 2: Test `POLYMARKET_VARIANT=legacy`** — boot with variant=legacy and mocked `POLY_PRIVATE_KEY`/`POLY_FUNDER`; assert the legacy `PolymarketCollector` is instantiated (not the US one). No live HTTP — use the existing sandbox fixture harness.

- [ ] **Step 3: Test switch-under-load** — start under `us` variant, kill, relaunch under `legacy`, confirm clean transition with no orphan connections.

- [ ] **Step 4: Commit.**

(Insert this task between Task 19 (Playwright onboarding) and Task 20 (Full suite). Renumber downstream tasks accordingly.)

## Task renumbering after insertion

Task 20 → 21 (Full suite); Task 21 → 22 (Telegram deliverable).
