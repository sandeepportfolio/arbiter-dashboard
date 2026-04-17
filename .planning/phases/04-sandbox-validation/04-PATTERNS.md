# Phase 4: Sandbox Validation - Pattern Map

**Mapped:** 2026-04-16
**Files analyzed:** 17 (14 new + 3 modified)
**Analogs found:** 15 / 17 (2 have no direct analog: init-multiple-dbs.sh, .env.sandbox.template — see §No Analog Found)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `arbiter/sandbox/__init__.py` | package marker | n/a | `arbiter/safety/__init__.py` | exact |
| `arbiter/sandbox/conftest.py` | test-fixtures | request-response | `arbiter/safety/conftest.py` + root `conftest.py` | exact (both) |
| `arbiter/sandbox/test_kalshi_happy_path.py` | live scenario test | request-response | `arbiter/safety/test_supervisor.py` (async-test shape) + `arbiter/execution/test_engine.py` (engine construction) | role-match |
| `arbiter/sandbox/test_polymarket_happy_path.py` | live scenario test | request-response | `arbiter/safety/test_supervisor.py` + `arbiter/execution/test_engine.py` | role-match |
| `arbiter/sandbox/test_fok_rejection.py` | live scenario test | request-response | `arbiter/safety/test_supervisor.py` | role-match |
| `arbiter/sandbox/test_timeout_cancel.py` | live scenario test | request-response | `arbiter/safety/test_supervisor.py` | role-match |
| `arbiter/sandbox/test_kill_switch.py` | live scenario test | event-driven | `arbiter/safety/test_supervisor.py::test_trip_kill_cancels_all` | exact |
| `arbiter/sandbox/test_one_leg_exposure.py` | live scenario test (fault-injected) | event-driven | `arbiter/safety/test_supervisor.py::test_handle_one_leg_exposure_sends_telegram_and_publishes` | exact |
| `arbiter/sandbox/test_rate_limit_burst.py` | live scenario test (fault-injected) | event-driven | `arbiter/test_api_integration.py::_make_rate_limit_api` + `test_rate_limit_ws_event_shape` | exact |
| `arbiter/sandbox/test_graceful_shutdown.py` | live scenario test (subprocess) | event-driven | `arbiter/test_api_integration.py::test_api_and_dashboard_contracts` (subprocess pattern lines 35-46, 219-224) | exact |
| `arbiter/sandbox/evidence.py` | test utility | file-I/O | `arbiter/execution/store.py::get_order` (SQL fetch) + RESEARCH.md §Pattern 5 | partial (compose from two) |
| `arbiter/sandbox/README.md` | documentation | n/a | `.env.template` (format style) — **no package-level README exists** | no-analog |
| `.env.sandbox.template` | config template | n/a | `.env.template` | exact |
| `docker/postgres/init-multiple-dbs.sh` | infra script | file-I/O | **none in repo** — reference RESEARCH.md §docker-compose multi-database init | no-analog |
| `arbiter/config/settings.py:365,376` (MOD) | config | request-response | `arbiter/config/settings.py:367,378,383,384` (existing `field(default_factory=lambda: os.getenv(...))` pattern) | exact (same file) |
| `arbiter/execution/adapters/polymarket.py::place_fok` (MOD) | adapter surgical edit | request-response | `arbiter/execution/adapters/polymarket.py:72-89` (existing early-return guards in same method) | exact (same method) |
| `docker-compose.yml` (MOD) | infra config | n/a | `docker-compose.yml` (existing postgres service block, lines 12-29) | exact (same file) |

## Pattern Assignments

### `arbiter/sandbox/__init__.py` (package marker)

**Analog:** `arbiter/safety/__init__.py`

Empty package marker — one-line docstring optional. No imports needed at package level; fixtures live in `conftest.py`.

---

### `arbiter/sandbox/conftest.py` (fixtures, `--live` opt-in)

**Primary analog:** `arbiter/safety/conftest.py`
**Secondary analog:** `conftest.py` (root — async runner hook; DO NOT duplicate)

**Imports pattern** (from `arbiter/safety/conftest.py:1-6`):
```python
"""Shared fixtures for arbiter.safety tests."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
```

**Fixture factory pattern** (from `arbiter/safety/conftest.py:17-27`):
```python
@pytest.fixture
def fake_adapter_factory():
    """Returns a factory producing AsyncMock adapters with a cancel_all method."""

    def make(platform: str, cancelled_ids: list[str] | None = None):
        adapter = AsyncMock()
        adapter.platform = platform
        adapter.cancel_all = AsyncMock(return_value=list(cancelled_ids or []))
        return adapter

    return make
```

**Async test dispatch pattern** (from root `conftest.py:1-19`) — **DO NOT redefine this hook in sandbox conftest; it is globally active.** Sandbox tests are plain `async def` without any marker. RESEARCH.md Anti-Pattern: "do NOT add `@pytest.mark.asyncio`".
```python
import asyncio
import inspect


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run the test inside an asyncio event loop")


def pytest_pyfunc_call(pyfuncitem):
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None

    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(test_func(**kwargs))
    return True
```

**New code to ADD to `arbiter/sandbox/conftest.py`** (from RESEARCH.md §Pattern 1, not in codebase yet):
```python
def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="...")

def pytest_configure(config):
    config.addinivalue_line("markers", "live: Phase 4 sandbox live-fire scenario")

def pytest_collection_modifyitems(config, items):
    if config.getoption("--live") or config.getoption("-m") == "live":
        return
    skip_live = pytest.mark.skip(reason="Use -m live or --live to run Phase 4 scenarios")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
```

**Fixture guard-rail pattern** (new — enforce safety assertions per RESEARCH.md Anti-Patterns):
```python
@pytest.fixture
async def sandbox_db():
    import os
    url = os.getenv("DATABASE_URL", "")
    assert "arbiter_sandbox" in url, (
        f"SAFETY: DATABASE_URL must point at arbiter_sandbox DB; got {url!r}"
    )
    # ... asyncpg.create_pool(url) ...

@pytest.fixture
def poly_test_adapter(...):
    assert os.getenv("PHASE4_MAX_ORDER_USD"), (
        "SAFETY: PHASE4_MAX_ORDER_USD must be set before building Polymarket adapter"
    )
    # ...
```

---

### `arbiter/sandbox/test_kalshi_happy_path.py` / `test_polymarket_happy_path.py` / `test_fok_rejection.py` / `test_timeout_cancel.py` (scenario tests, `real` tag)

**Analog (test shape):** `arbiter/safety/test_supervisor.py`

**Async-test pattern (no marker)** (from `arbiter/safety/test_supervisor.py:48-61`):
```python
async def test_trip_kill_cancels_all(fake_notifier, fake_adapter_factory):
    adapters = {
        "kalshi": fake_adapter_factory("kalshi", ["k1", "k2", "k3"]),
        "polymarket": fake_adapter_factory("polymarket", ["p1", "p2"]),
    }
    supervisor = _build_supervisor(adapters, fake_notifier)
    state = await asyncio.wait_for(
        supervisor.trip_kill(by="operator:test", reason="manual"),
        timeout=5.0,
    )
    assert state.armed is True
    adapters["kalshi"].cancel_all.assert_awaited_once()
```

**Live marker + fixture-injected adapter pattern** (sandbox-specific, NEW; follows shape from safety/test_supervisor.py):
```python
@pytest.mark.live
async def test_kalshi_happy_lifecycle(demo_kalshi_adapter, sandbox_db, evidence_dir):
    # ... exercise adapter.place_fok against demo-api.kalshi.co ...
    # ... assert Order.status == OrderStatus.FILLED ...
    # ... pull fill via GET /portfolio/fills, assert fee_cost ≈ kalshi_order_fee() ±1¢ ...
```

**Engine construction pattern** (from `arbiter/execution/test_engine.py:13-27`):
```python
def make_engine(price_store: PriceStore) -> ExecutionEngine:
    config = ArbiterConfig()
    config.scanner.dry_run = True
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.safety.max_platform_exposure_usd = 1_000_000.0
    monitor = BalanceMonitor(config.alerts, {"kalshi": object(), "polymarket": object(), "predictit": object()})
    engine = ExecutionEngine(config, monitor, price_store=price_store, collectors={})
    engine.risk._max_daily_trades = 250
    engine.risk._max_total_exposure = 50_000
    return engine
```

**FOK rejection assertion pattern** — RESEARCH.md Pitfall 3 says: assert on `Order.status == OrderStatus.CANCELLED`, NOT on HTTP status. `KalshiAdapter._FOK_STATUS_MAP` (arbiter/execution/adapters/kalshi.py:27) already maps response `status: canceled` → `OrderStatus.CANCELLED`.

---

### `arbiter/sandbox/test_kill_switch.py` (SAFE-01 live-fire, `real` tag)

**Analog:** `arbiter/safety/test_supervisor.py::test_trip_kill_cancels_all` (lines 48-61)

Same shape as above — but use **real** (demo-configured) `KalshiAdapter` instead of `fake_adapter_factory`. Trip supervisor → assert adapter.cancel_all is invoked AND that the demo Kalshi order actually moves to CANCELLED on the exchange (verify via `adapter.get_order(order_id)`).

---

### `arbiter/sandbox/test_one_leg_exposure.py` (SAFE-03 injected, `injected` tag)

**Analog:** `arbiter/safety/test_supervisor.py::test_handle_one_leg_exposure_sends_telegram_and_publishes` (lines 143-193)

**Event-subscribe assertion pattern** (from `arbiter/safety/test_supervisor.py:151-193`):
```python
async def test_handle_one_leg_exposure_sends_telegram_and_publishes(
    fake_notifier, fake_adapter_factory,
):
    supervisor = _build_supervisor(adapters, fake_notifier)
    queue = supervisor.subscribe()
    # ... invoke supervisor.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp) ...
    fake_notifier.send.assert_awaited_once()
    sent_message = fake_notifier.send.await_args.args[0]
    assert "NAKED POSITION" in sent_message

    event = queue.get_nowait()
    assert event["type"] == "one_leg_exposure"
    payload = event.get("payload", {})
    assert payload.get("canonical_id") == "MKT1"
```

**Fault-injection pattern** (NEW — RESEARCH.md §Pattern 3):
```python
@pytest.mark.live
async def test_one_leg_recovery_injected(
    demo_kalshi_adapter, poly_test_adapter, monkeypatch, evidence_dir
):
    call_count = {"n": 0}
    original = poly_test_adapter.place_fok

    async def flaky_place_fok(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("INJECTED: simulated Polymarket failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(poly_test_adapter, "place_fok", flaky_place_fok)
    # ... run arb through engine, observe one_leg_exposure event + unwind ...
```

---

### `arbiter/sandbox/test_rate_limit_burst.py` (SAFE-04 injected, `injected` tag)

**Analog:** `arbiter/test_api_integration.py::_make_rate_limit_api` (lines 230-276) + `test_rate_limit_ws_event_shape` (lines 279-309)

**In-process RateLimiter + WS assertion pattern** (from `arbiter/test_api_integration.py:245-266`):
```python
from arbiter.utils.retry import RateLimiter

kalshi_rl = RateLimiter(name="kalshi-exec", max_requests=10, window_seconds=1.0)
poly_rl = RateLimiter(name="poly-exec", max_requests=5, window_seconds=1.0)

kalshi_adapter = SimpleNamespace(rate_limiter=kalshi_rl)
poly_adapter = SimpleNamespace(rate_limiter=poly_rl)
# ... wire through ArbiterAPI, start _rate_limit_broadcast_loop, connect WS,
# observe rate_limit_state event with remaining_penalty_seconds > 0 ...
```

Flood `RateLimiter.acquire()` from the test to trip burst threshold (test-owned sink — no production code change).

---

### `arbiter/sandbox/test_graceful_shutdown.py` (SAFE-05 subprocess, `real` tag)

**Analog:** `arbiter/test_api_integration.py::test_api_and_dashboard_contracts` (subprocess block lines 35-46, teardown lines 219-224)

**Subprocess launch pattern** (from `arbiter/test_api_integration.py:14-46`):
```python
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"Local socket binding unavailable in this sandbox: {exc}")
        return sock.getsockname()[1]


def wait_for_server(port: int, timeout: float = 15.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise AssertionError(f"Server on port {port} did not become ready")


def test_api_and_dashboard_contracts():
    port = free_port()
    env = dict(os.environ)
    env["ARBITER_UI_SMOKE_SEED"] = "1"
    env["DRY_RUN"] = "true"
    proc = subprocess.Popen(
        [sys.executable, "-m", "arbiter.main", "--api-only", "--port", str(port)],
        cwd=os.getcwd(), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        wait_for_server(port)
        # ... test body ...
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

**SAFE-05 adaptation (NEW):** After `wait_for_server`, place demo Kalshi order via WS/HTTP, then `os.kill(proc.pid, signal.SIGINT)`. Capture `proc.stdout`/`proc.stderr` into `evidence_dir/run.log.jsonl`. Assert subprocess emits `shutdown_state` with `phase=shutting_down` event before exit.

---

### `arbiter/sandbox/evidence.py` (evidence capture helper)

**Analog (DB dump):** `arbiter/execution/store.py::get_order` (lines 154-161)
**Analog (reconciliation):** `arbiter/audit/pnl_reconciler.py::reconcile` (lines 141-180)

**DB fetch pattern** (from `arbiter/execution/store.py:154-172`):
```python
async def get_order(self, order_id: str) -> Optional[Order]:
    if self._pool is None:
        await self.connect()
    async with self._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM execution_orders WHERE order_id = $1", order_id
        )
    return self._row_to_order(row)

async def list_non_terminal_orders(self) -> List[Order]:
    if self._pool is None:
        await self.connect()
    async with self._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM execution_orders "
            "WHERE status IN ('pending', 'submitted', 'partial') "
            "ORDER BY submitted_at ASC"
        )
    return [self._row_to_order(r) for r in rows if r is not None]
```

**Evidence-dump adaptation (NEW, from RESEARCH.md §Pattern 5):**
```python
async def dump_execution_tables(pool, directory: pathlib.Path) -> None:
    for table in ("execution_orders", "execution_fills", "execution_incidents", "execution_arbs"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT * FROM {table}")
        (directory / f"{table}.json").write_text(
            json.dumps([dict(r) for r in rows], indent=2, default=str),
            encoding="utf-8",
        )
```

**Balance snapshot pattern (reuse existing, no-new-code path):**
```python
from arbiter.monitor.balance import BalanceMonitor
# snapshot = await monitor.check_balances()
# (directory / "balances_pre.json").write_text(
#     json.dumps({p: {"balance": s.balance, "timestamp": s.timestamp} for p, s in snapshot.items()}),
# )
```

**Reconciliation assertion pattern** (from `arbiter/audit/pnl_reconciler.py:141-180`):
```python
def reconcile(self, current_balances: Dict[str, float]) -> ReconciliationReport:
    # ... for each platform:
    expected = starting + recorded
    discrepancy = expected - actual_balance
    is_flagged = abs(discrepancy) > self.threshold  # THIS threshold pattern → ±1¢ in sandbox
```
Sandbox assertion: call `pnl_reconciler.reconcile(post_balances)` with `threshold=0.01` and assert `not report.has_flags`.

---

### `arbiter/sandbox/README.md` (operator bootstrap)

**No direct analog in codebase.** No package-level READMEs exist (`arbiter/**/README*` glob returned none).

**Use format style from `.env.template`:**
- `# ── Section ───` headings
- Inline comments explaining each variable's purpose
- Copy-to-fill pattern

**Planner to source content from RESEARCH.md §Environment Availability** (demo account provisioning, test-card funding, USDC bridging).

---

### `.env.sandbox.template` (credential template)

**Analog:** `.env.template` (exact shape)

**Section-header pattern** (from `.env.template:1-52`):
```bash
# ARBITER — Environment Variables
# Copy to .env and fill in your values

# ── Core ────────────────────────────────────────────────────────────────────
# Set to 'false' ONLY when all credential flows are tested and verified.
DRY_RUN=true

# ── Postgres ────────────────────────────────────────────────────────────────
# Full connection string. Can also use individual PG_* vars.
DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_dev

# ── Kalshi ─────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_private.pem

# ── Polymarket ──────────────────────────────────────────────────────────────
POLY_PRIVATE_KEY=
```

**Sandbox overrides required (from RESEARCH.md §Code Examples):**
- `DATABASE_URL` → `arbiter_sandbox`
- `KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2`
- `KALSHI_WS_URL=wss://demo-api.kalshi.co/trade-api/ws/v2`
- `KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_demo_private.pem`
- `POLYMARKET_CLOB_URL=https://clob.polymarket.com`
- `POLY_PRIVATE_KEY=<TEST-WALLET-PRIVATE-KEY-ONLY>`
- `PHASE4_MAX_ORDER_USD=5`

---

### `docker/postgres/init-multiple-dbs.sh` (infra script)

**No analog in repo.** Reference RESEARCH.md §docker-compose multi-database init (verbatim pattern from `mrts/docker-postgresql-multiple-databases`).

**Target location:** repo convention is `arbiter/sql/init-sandbox.sh` (per RESEARCH.md line 543), mounted to `/docker-entrypoint-initdb.d/init-sandbox.sh`. Planner should harmonize file location with existing `arbiter/sql/init.sql` mount point in `docker-compose.yml:23`.

---

### MOD: `arbiter/config/settings.py:365, 376` (env-var-driven defaults)

**Same-file analog:** lines 367, 378, 383, 384 already use `field(default_factory=lambda: os.getenv(...))` pattern.

**Existing pattern (line 367):**
```python
api_key_id: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID", ""))
```

**Existing pattern with non-empty default (line 383):**
```python
signature_type: int = field(default_factory=lambda: int(os.getenv("POLY_SIGNATURE_TYPE", "2")))
```

**Edit to apply (RESEARCH.md line 457-484):**
```python
# arbiter/config/settings.py:365
base_url: str = field(
    default_factory=lambda: os.getenv(
        "KALSHI_BASE_URL",
        "https://api.elections.kalshi.com/trade-api/v2",
    )
)
ws_url: str = field(
    default_factory=lambda: os.getenv(
        "KALSHI_WS_URL",
        "wss://api.elections.kalshi.com/trade-api/ws/v2",
    )
)

# arbiter/config/settings.py:376
clob_url: str = field(
    default_factory=lambda: os.getenv(
        "POLYMARKET_CLOB_URL",
        "https://clob.polymarket.com",
    )
)
```

Pattern is identical to neighboring fields; surgical 2-line-type edit.

---

### MOD: `arbiter/execution/adapters/polymarket.py::place_fok` (PHASE4_MAX_ORDER_USD hard-lock)

**Same-file analog:** lines 72-89 (existing early-return guards in `place_fok`).

**Existing guard pattern (lines 72-89):**
```python
async def place_fok(
    self, arb_id, market_id, canonical_id, side, price, qty,
) -> Order:
    now = time.time()

    if not getattr(self.config.polymarket, "private_key", None):
        return self._failed_order(
            arb_id, market_id, canonical_id, side, price, qty,
            now, "Polymarket wallet not configured",
        )

    if not self.circuit.can_execute():
        return self._failed_order(
            arb_id, market_id, canonical_id, side, price, qty,
            now, "polymarket circuit open",
        )

    client = self._get_client()
    if client is None:
        return self._failed_order(
            arb_id, market_id, canonical_id, side, price, qty,
            now, "Unable to initialize Polymarket client",
        )
```

**`_failed_order` helper pattern (lines 632-648):**
```python
def _failed_order(
    self, arb_id, market_id, canonical_id, side, price, qty, ts, error: str,
) -> Order:
    return Order(
        order_id=f"{arb_id}-{side.upper()}-POLY",
        platform="polymarket",
        market_id=market_id,
        canonical_id=canonical_id,
        side=side,
        price=price,
        quantity=qty,
        status=OrderStatus.FAILED,
        timestamp=ts,
        error=error,
        external_client_order_id=None,
    )
```

**Insertion point:** between the `client is None` check (line 89) and `return await self._place_fok_reconciling(...)` (line 91).

**Code to insert (RESEARCH.md lines 568-586, NOTIONAL check per Pitfall 8):**
```python
max_order_usd_raw = os.getenv("PHASE4_MAX_ORDER_USD")
if max_order_usd_raw:
    try:
        max_order_usd = float(max_order_usd_raw)
    except (TypeError, ValueError):
        max_order_usd = 0.0
    notional_usd = float(qty) * float(price)
    if notional_usd > max_order_usd:
        log.warning(
            "polymarket.phase4_hardlock.rejected",
            arb_id=arb_id, notional=notional_usd, max=max_order_usd,
            qty=qty, price=price,
        )
        return self._failed_order(
            arb_id, market_id, canonical_id, side, price, qty, now,
            f"PHASE4_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
        )
```

Requires adding `import os` at top of file if not already present.

**Structlog logger pattern already established (line 32):**
```python
log = structlog.get_logger("arbiter.adapters.polymarket")
```
Use this same `log` instance for the hard-lock warning event — keyword-argument structured logging style matches existing code (see `_place_fok_reconciling` for many examples).

---

### MOD: `docker-compose.yml` (add sandbox DB)

**Same-file analog:** postgres service block (lines 12-29).

**Existing postgres service (lines 12-29):**
```yaml
postgres:
  image: postgres:16-alpine
  container_name: arbiter-postgres
  environment:
    POSTGRES_DB: ${PG_DATABASE:-arbiter_dev}
    POSTGRES_USER: ${PG_USER:-arbiter}
    POSTGRES_PASSWORD: ${PG_PASSWORD:-arbiter_secret}
  ports:
    - "${PG_PORT:-5432}:5432"
  volumes:
    - pg_data:/var/lib/postgresql/data
    - ./arbiter/sql/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${PG_USER:-arbiter}"]
    interval: 10s
    timeout: 5s
    retries: 5
  restart: unless-stopped
```

**Edit pattern (RESEARCH.md lines 521-539):** Add env var + mount second init script:
```yaml
environment:
  POSTGRES_DB: ${PG_DATABASE:-arbiter_dev}
  POSTGRES_USER: ${PG_USER:-arbiter}
  POSTGRES_PASSWORD: ${PG_PASSWORD:-arbiter_secret}
  POSTGRES_MULTIPLE_DATABASES: arbiter_sandbox   # NEW
volumes:
  - pg_data:/var/lib/postgresql/data
  - ./arbiter/sql/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
  - ./arbiter/sql/init-sandbox.sh:/docker-entrypoint-initdb.d/init-sandbox.sh:ro  # NEW
```

Existing mount convention (line 23) points to `./arbiter/sql/init.sql` — new init script should live in `arbiter/sql/init-sandbox.sh` for consistency (NOT `docker/postgres/`, which does not exist as a directory in the repo). Planner should reconcile.

---

## Shared Patterns

### Structlog structured logging
**Source:** `arbiter/execution/adapters/polymarket.py:28,32` + `arbiter/utils/logger.py`
**Apply to:** evidence.py, all scenario tests, polymarket.py hard-lock edit
```python
import structlog
log = structlog.get_logger("arbiter.sandbox.<module>")
# Usage: log.warning("polymarket.phase4_hardlock.rejected", arb_id=..., notional=..., max=...)
```

### asyncpg pool + acquire + fetch
**Source:** `arbiter/execution/store.py:60-79, 154-172`
**Apply to:** `arbiter/sandbox/evidence.py` (DB table dumps), any sandbox fixture that reads from `arbiter_sandbox`
```python
async with self._pool.acquire() as conn:
    rows = await conn.fetch("SELECT * FROM <table>")
```

### AsyncMock adapter construction for fault injection
**Source:** `arbiter/safety/conftest.py:17-27`, `arbiter/test_api_integration.py:247-264`
**Apply to:** test_one_leg_exposure.py, test_rate_limit_burst.py
Pattern: wrap real adapter method with `monkeypatch.setattr(adapter, "method_name", wrapper)` that counts calls and raises on target call number.

### Subprocess lifecycle (launch → wait_for_server → signal → teardown)
**Source:** `arbiter/test_api_integration.py:14-46, 219-224`
**Apply to:** test_graceful_shutdown.py ONLY (per RESEARCH.md Open Question 4: in-process for SAFE-01/03/04, subprocess for SAFE-05)

### `field(default_factory=lambda: os.getenv(...))` config pattern
**Source:** `arbiter/config/settings.py:367, 378, 383, 384`
**Apply to:** MOD at settings.py:365, 376 — keep production defaults, env-var overrides.

### `_failed_order(...)` early-return guard
**Source:** `arbiter/execution/adapters/polymarket.py:73, 79, 86, 632-648`
**Apply to:** MOD at polymarket.py::place_fok PHASE4_MAX_ORDER_USD hard-lock — reuse the same helper; do NOT invent a new failure path.

## No Analog Found

Files with no close match in the codebase (planner should use RESEARCH.md patterns instead):

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `docker/postgres/init-multiple-dbs.sh` (or `arbiter/sql/init-sandbox.sh`) | infra script | file-I/O | No shell scripts in repo for Postgres initdb; use RESEARCH.md §docker-compose multi-database init verbatim. |
| `arbiter/sandbox/README.md` | operator docs | n/a | No package-level READMEs exist. Use `.env.template` comment-style + RESEARCH.md §Environment Availability for content. Planner must author from scratch. |

## Metadata

**Analog search scope:**
- `arbiter/safety/` — conftest + test shapes
- `arbiter/execution/` — adapter edit site, store SQL patterns, test_engine.py engine construction
- `arbiter/audit/` — reconciliation helpers
- `arbiter/monitor/balance.py` — BalanceMonitor reuse
- `arbiter/test_api_integration.py` — subprocess + in-process RateLimiter patterns
- `arbiter/config/settings.py` — env-var override pattern
- `arbiter/utils/logger.py` — structlog binding
- `docker-compose.yml`, `.env.template`, `arbiter/sql/` — infra shapes
- Root `conftest.py` — async test dispatch (DO NOT REDEFINE in sandbox conftest)

**Files scanned:** 14 (read in full or with targeted grep); 2 glob sweeps (`**/conftest.py`, `arbiter/**/README*`).

**Pattern extraction date:** 2026-04-16

*Phase: 04-sandbox-validation*
