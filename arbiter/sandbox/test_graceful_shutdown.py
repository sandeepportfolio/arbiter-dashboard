"""SAFE-05 graceful-shutdown subprocess test (Scenario 9: real SIGINT against running server).

This test launches `python -m arbiter.main --api-only --port <free>` as a subprocess using the
operator-sourced `.env.sandbox` environment, places a resting Kalshi demo order from the
in-test harness, sends SIGINT (or CTRL_BREAK_EVENT on Windows), and asserts:

1. The subprocess emits structured-log events describing the shutdown sequence
   (logger.info("Preparing safety-supervised shutdown...") + "Stopping all components..."
   + "ARBITER shutdown complete") — these fire in arbiter.main::run_shutdown_sequence and
   bookend the supervisor's `shutdown_state` pub/sub fanout (phase=shutting_down ->
   trip_kill -> phase=complete).
2. The resting demo order is CANCELLED on the demo exchange (verified via a post-shutdown
   `list_open_orders_by_client_id` query from the in-test adapter — because the subprocess's
   cancel_all enumerates SERVER-SIDE orders via `_list_all_open_orders`, an order placed by
   the test harness's independent adapter IS visible to the subprocess on shutdown).
3. The subprocess exits with a non-negative return code (not forced-kill).

Scope note (per Plan 04-07 Task 1 action — NON-NEGOTIABLE):
This test places a NON-FOK limit order via the in-test KalshiAdapter. The adapter's only
public placement method is `place_fok` (FOK), which will not rest. Resolution rule (3-step,
identical to Plan 04-03 Task 3):

  1. If KalshiAdapter exposes a public non-FOK method (place_limit / place_gtc /
     place_resting_limit) -- USE IT.
  2. Else if adapter._client.create_order(...) is available -- use it as a TEST-ONLY
     bypass. Bypasses adapter FOK invariant for test setup only; production adapter is
     not modified. Scope boundary: Plan 04-07 is test-only; production code changes
     are forbidden in this plan.
  3. Else (current state as of 2026-04-17: KalshiAdapter is an aiohttp-based HTTP wrapper
     without a `_client` SDK attribute) -- fall through to a TEST-ONLY raw-HTTP POST path
     that reuses the adapter's existing session + auth + config plumbing. Reusing existing
     adapter state (session, auth, config) is NOT the same as adding a new public method to
     KalshiAdapter -- the adapter surface remains frozen.
  4. Last-resort escape hatch: pytest.fail(...) with a plan-revision request if even raw
     HTTP POST via adapter.session is unworkable.

FORBIDDEN: Adding a public non-FOK method to KalshiAdapter in this plan.

Production adapter is not modified. Scope boundary: Plan 04-07 is test-only; production
code changes are forbidden in this plan.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from types import SimpleNamespace

import pytest
import structlog

log = structlog.get_logger("arbiter.sandbox.shutdown")

# Operator-supplied from Task 0 (`approved: shutdown_market=<TICKER>, ...`). Kept as a
# clearly-marked placeholder so the test fails loudly rather than silently hitting the
# wrong Kalshi demo market if the operator skips the checkpoint.
SHUTDOWN_MARKET_TICKER = os.getenv("PHASE4_SHUTDOWN_TICKER", "REPLACE-WITH-OPERATOR-SUPPLIED-TICKER")
SHUTDOWN_PRICE = float(os.getenv("PHASE4_SHUTDOWN_PRICE", "0.05"))
SHUTDOWN_QTY = int(os.getenv("PHASE4_SHUTDOWN_QTY", "3"))


def _free_port() -> int:
    """Pattern from arbiter/test_api_integration.py:14-20."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"Local socket bind unavailable: {exc}")
        return sock.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 20.0) -> None:
    """Pattern from arbiter/test_api_integration.py:23-32."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1,
            ) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise AssertionError(
        f"Server on port {port} did not become ready within {timeout}s",
    )


async def _place_resting_limit_via_adapter_or_bypass(
    adapter,
    *,
    arb_id: str,
    market_id: str,
    side: str,
    price: float,
    qty: int,
    client_order_id: str,
):
    """Non-FOK resting order placement -- 3-step resolution rule (Plan 04-03 Task 3).

    1) Use existing PUBLIC non-FOK adapter method if present.
    2) Else use adapter._client.create_order(...) as a TEST-ONLY bypass.
       Bypasses adapter FOK invariant for test setup only; production adapter is not
       modified. Scope boundary: Plan 04-07 is test-only; production code changes are
       forbidden in this plan.
    3) Else fall through to a TEST-ONLY raw-HTTP POST using adapter.session + adapter.auth
       + adapter.config -- this reuses EXISTING adapter plumbing (not adding new public
       methods) to submit a GTC (time_in_force=resting) limit order against demo Kalshi.
       The adapter surface remains frozen.
    4) Else pytest.fail(...) requesting a plan revision.

    Returns a SimpleNamespace with `order_id` and `client_order_id`.
    """
    # Step 1: look for a public non-FOK placement method.
    for name in ("place_limit", "place_gtc", "place_resting_limit"):
        fn = getattr(adapter, name, None)
        if callable(fn):
            # G-2 fix (Plan 04-09, 2026-04-20): KalshiAdapter.place_resting_limit
            # does NOT accept a client_order_id kwarg — the adapter generates
            # its own internally and surfaces it as Order.external_client_order_id.
            # The Phase 4.1 adapter signature is frozen; tests must consume the
            # adapter-generated id rather than pass one in. Step 2/Step 3 below
            # are TEST-ONLY raw-HTTP bypass paths that DO use the test-generated
            # client_order_id because they skip the adapter's generator entirely.
            order = await fn(
                arb_id=arb_id,
                market_id=market_id,
                canonical_id=market_id,
                side=side,
                price=price,
                qty=qty,
            )
            return SimpleNamespace(
                order_id=str(order.order_id),
                client_order_id=str(order.external_client_order_id or ""),
                raw=order,
            )

    # Step 2: TEST-ONLY bypass via adapter._client.create_order(...).
    # Bypasses adapter FOK invariant for test setup only; production adapter is not
    # modified. Scope boundary: Plan 04-07 is test-only; production code changes are
    # forbidden in this plan.
    client = getattr(adapter, "_client", None)
    if client is not None and hasattr(client, "create_order"):
        fn = client.create_order
        if asyncio.iscoroutinefunction(fn):
            raw = await fn(
                ticker=market_id,
                side=side,
                count=qty,
                price=price,
                type="limit",
                time_in_force="GTC",
                client_order_id=client_order_id,
            )
        else:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None,
                lambda: fn(
                    ticker=market_id,
                    side=side,
                    count=qty,
                    price=price,
                    type="limit",
                    time_in_force="GTC",
                    client_order_id=client_order_id,
                ),
            )
        order_id = (
            getattr(raw, "order_id", None)
            or (raw.get("order_id") if isinstance(raw, dict) else None)
            or (
                raw.get("order", {}).get("order_id")
                if isinstance(raw, dict)
                else None
            )
        )
        if not order_id:
            pytest.fail(
                f"Plan 04-07 Task 1: Kalshi SDK create_order returned no order_id. "
                f"Raw response: {raw!r}. Adjust wrapping to match SDK shape."
            )
        return SimpleNamespace(
            order_id=order_id, client_order_id=client_order_id, raw=raw,
        )

    # Step 3: TEST-ONLY raw-HTTP POST using adapter.session + adapter.auth + adapter.config.
    # This reuses EXISTING adapter plumbing (not a new public method) -- the adapter surface
    # remains frozen. Production adapter is not modified. Scope boundary: Plan 04-07 is
    # test-only; production code changes are forbidden in this plan.
    session = getattr(adapter, "session", None)
    auth = getattr(adapter, "auth", None)
    config = getattr(adapter, "config", None)
    if session is not None and auth is not None and config is not None:
        if not getattr(auth, "is_authenticated", False):
            pytest.fail(
                "Plan 04-07 Task 1: adapter.auth is not authenticated on demo Kalshi. "
                "Check KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in .env.sandbox."
            )
        base_url = getattr(config.kalshi, "base_url", None) or getattr(
            config, "kalshi_base_url", None,
        )
        if not base_url:
            pytest.fail(
                "Plan 04-07 Task 1: cannot resolve Kalshi base_url from adapter.config; "
                "inspect config.kalshi.base_url."
            )
        body = {
            "ticker": market_id,
            "client_order_id": client_order_id,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count_fp": f"{float(qty):.2f}",
            "time_in_force": "resting",  # GTC-equivalent on Kalshi (non-FOK)
        }
        if side == "yes":
            body["yes_price_dollars"] = f"{price:.4f}"
        else:
            body["no_price_dollars"] = f"{price:.4f}"
        path = "/trade-api/v2/portfolio/orders"
        url = f"{base_url}/portfolio/orders"
        try:
            headers = auth.get_headers("POST", path)
        except Exception as exc:
            pytest.fail(
                f"Plan 04-07 Task 1: auth.get_headers failed: {exc!r}. "
                f"Cannot submit resting order via TEST-ONLY raw-HTTP path."
            )
        async with session.post(url, json=body, headers=headers) as response:
            status = response.status
            payload = await response.text()
        if status not in (200, 201):
            pytest.fail(
                f"Plan 04-07 Task 1: TEST-ONLY raw-HTTP resting-order POST returned "
                f"status={status}, body={payload[:400]!r}. Adjust body shape or check "
                f"demo Kalshi market liquidity + funding."
            )
        try:
            data = json.loads(payload)
        except Exception:
            data = {}
        order_data = data.get("order", data) if isinstance(data, dict) else {}
        order_id = (
            order_data.get("order_id")
            if isinstance(order_data, dict)
            else None
        )
        if not order_id:
            pytest.fail(
                f"Plan 04-07 Task 1: TEST-ONLY raw-HTTP POST succeeded but response "
                f"lacked order_id. body={payload[:400]!r}"
            )
        return SimpleNamespace(
            order_id=str(order_id), client_order_id=client_order_id, raw=data,
        )

    pytest.fail(
        "Plan 04-07 Task 1: KalshiAdapter exposes no public non-FOK placement method AND "
        "adapter._client.create_order is not available AND adapter.session/auth/config are "
        "not wired. Cannot live-fire SAFE-05 cancel-on-shutdown without a resting order. "
        "Request plan revision: either Plan 04-02 gains an explicit scope expansion to add "
        "KalshiAdapter.place_resting_limit (with tests), or re-scope this scenario to "
        "validate SAFE-05 via an already-resting order placed by the subprocess itself. "
        "Adding a public method to KalshiAdapter in this plan is FORBIDDEN."
    )


@pytest.mark.live
async def test_sigint_cancels_open_kalshi_demo_orders(evidence_dir):
    """Launch arbiter.main as subprocess, place resting Kalshi demo order, SIGINT, verify graceful shutdown.

    Scope note: This test places a NON-FOK limit order via the in-test KalshiAdapter.
    Resolution per plan Task 1 action: existing public non-FOK adapter method IF available,
    else `adapter._client.create_order(...)` as a TEST-ONLY bypass (not present on current
    HTTP-based KalshiAdapter), else a TEST-ONLY raw-HTTP POST path that reuses the adapter's
    existing session + auth + config. Bypasses adapter FOK invariant for test setup only;
    production adapter is not modified. Scope boundary: Plan 04-07 is test-only; production
    code changes are forbidden in this plan.

    SAFE-05 invariant verified: subprocess running arbiter.main --api-only receives SIGINT,
    its SafetySupervisor.prepare_shutdown() fires trip_kill() which invokes each adapter's
    cancel_all(). KalshiAdapter.cancel_all enumerates SERVER-SIDE open orders via
    _list_all_open_orders() -- so an order placed by the test's independent adapter IS
    visible to the subprocess and WILL be cancelled during its shutdown sequence. The test
    verifies cancellation landed on the platform via a post-shutdown
    list_open_orders_by_client_id query.
    """
    # Pre-assertions: pointing at sandbox DB + demo Kalshi.
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), (
        f"SAFETY: DATABASE_URL must point at arbiter_sandbox DB; "
        f"got {os.getenv('DATABASE_URL', '')!r}"
    )
    assert "demo-api.kalshi.co" in os.getenv("KALSHI_BASE_URL", ""), (
        f"SAFETY: KALSHI_BASE_URL must point at demo-api.kalshi.co; "
        f"got {os.getenv('KALSHI_BASE_URL', '')!r}"
    )
    assert SHUTDOWN_MARKET_TICKER != "REPLACE-WITH-OPERATOR-SUPPLIED-TICKER", (
        "SAFETY: Task 0 operator checkpoint incomplete -- set PHASE4_SHUTDOWN_TICKER env "
        "var (or edit SHUTDOWN_MARKET_TICKER in this file) to a Kalshi demo market "
        "confirmed by operator in Task 0 resume signal."
    )

    port = _free_port()

    # Build subprocess env -- inherit current env (which sourced .env.sandbox) + explicit overrides.
    env = dict(os.environ)
    env["PHASE4_MAX_ORDER_USD"] = os.getenv("PHASE4_MAX_ORDER_USD", "5")

    # Capture stdout (where structlog logs land via arbiter.utils.logger.setup_logging) AND
    # stderr into evidence_dir. arbiter.main configures console handler on sys.stdout; stderr
    # may contain tracebacks if startup fails.
    stdout_path = evidence_dir / "run.log.jsonl"
    stderr_path = evidence_dir / "run.err.log"
    stdout_fh = open(stdout_path, "w", encoding="utf-8")
    stderr_fh = open(stderr_path, "w", encoding="utf-8")

    # Windows note: SIGINT delivery to a child requires CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [sys.executable, "-m", "arbiter.main", "--api-only", "--port", str(port)],
        cwd=os.getcwd(),
        env=env,
        stdout=stdout_fh,
        stderr=stderr_fh,
        creationflags=creationflags,
    )

    client_order_id: str | None = None
    shutdown_events_captured: list[dict] = []
    return_code: int | None = None
    adapter = None
    subprocess_start = time.time()
    shutdown_start: float | None = None
    non_fok_strategy = "unknown"

    try:
        _wait_for_server(port)
        subprocess_ready = time.time()
        log.info(
            "scenario.shutdown.server_ready",
            port=port, pid=proc.pid,
            startup_seconds=round(subprocess_ready - subprocess_start, 3),
        )

        # Place a resting Kalshi demo order using our own in-test adapter -- NOT via the
        # subprocess's API. The subprocess is the server being shutdown-tested; our test
        # harness places the order. Because KalshiAdapter.cancel_all enumerates server-side
        # orders via _list_all_open_orders(), the subprocess WILL see our order during its
        # shutdown cancel_all() pass.
        import aiohttp as _aiohttp

        from arbiter.collectors.kalshi import KalshiAuth
        from arbiter.config.settings import load_config
        from arbiter.execution.adapters.kalshi import KalshiAdapter
        from arbiter.utils.retry import CircuitBreaker, RateLimiter

        cfg = load_config()
        session = _aiohttp.ClientSession()
        auth = KalshiAuth(cfg.kalshi.api_key_id, cfg.kalshi.private_key_path)
        rate_limiter = RateLimiter(
            name="kalshi-exec-test", max_requests=10, window_seconds=1.0,
        )
        circuit = CircuitBreaker(
            name="kalshi-exec-test", failure_threshold=5, recovery_timeout=30.0,
        )
        adapter = KalshiAdapter(
            config=cfg, session=session, auth=auth,
            rate_limiter=rate_limiter, circuit=circuit,
        )
        client_order_id = f"ARB-SANDBOX-SHUTDOWN-YES-{os.urandom(4).hex()}"

        # Place non-FOK resting order via the 3-step resolution helper (public method OR
        # _client bypass OR TEST-ONLY raw-HTTP OR fail). Record which strategy was used.
        if any(
            hasattr(adapter, n)
            for n in ("place_limit", "place_gtc", "place_resting_limit")
        ):
            non_fok_strategy = "adapter_public_method"
        elif getattr(adapter, "_client", None) is not None and hasattr(
            getattr(adapter, "_client"), "create_order",
        ):
            non_fok_strategy = "adapter._client.create_order TEST-ONLY bypass"
        else:
            non_fok_strategy = "adapter.session + adapter.auth TEST-ONLY raw-HTTP"

        placed = await _place_resting_limit_via_adapter_or_bypass(
            adapter,
            arb_id="ARB-SANDBOX-SHUTDOWN",
            market_id=SHUTDOWN_MARKET_TICKER,
            side="yes",
            price=SHUTDOWN_PRICE,
            qty=SHUTDOWN_QTY,
            client_order_id=client_order_id,
        )
        # G-2 fix (Plan 04-09): if Step 1 (adapter.place_resting_limit) was chosen,
        # the adapter generated its own client_order_id — use the effective id
        # returned by the helper for downstream list_open_orders_by_client_id.
        # Step 2/Step 3 bypass paths return the test-generated id unchanged.
        effective_client_order_id = placed.client_order_id or client_order_id
        log.info(
            "scenario.shutdown.order_placed",
            order_id=placed.order_id,
            client_order_id=effective_client_order_id,
            non_fok_strategy=non_fok_strategy,
        )

        await asyncio.sleep(1.0)  # allow the order to rest on demo Kalshi

        # Send SIGINT (Unix) / CTRL_BREAK_EVENT (Windows).
        shutdown_start = time.time()
        log.info(
            "scenario.shutdown.sending_signal",
            pid=proc.pid,
            platform=sys.platform,
            signal=(
                "CTRL_BREAK_EVENT" if sys.platform == "win32" else "SIGINT"
            ),
        )
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.kill(proc.pid, signal.SIGINT)

        # Wait for subprocess to exit.
        try:
            return_code = proc.wait(timeout=20.0)
        except subprocess.TimeoutExpired:
            log.warning("scenario.shutdown.timeout_waiting_for_exit")
            proc.kill()
            return_code = proc.wait(timeout=5.0)
            pytest.fail(
                "Subprocess did not exit within 20s of SIGINT — SAFE-05 deadlock risk",
            )

        shutdown_duration = (
            time.time() - shutdown_start if shutdown_start else None
        )
        log.info(
            "scenario.shutdown.exited",
            return_code=return_code,
            shutdown_seconds=(
                round(shutdown_duration, 3)
                if shutdown_duration is not None
                else None
            ),
        )

        # Close log file handles and parse stdout JSONL for structlog events.
        stdout_fh.close()
        stderr_fh.close()

        # The subprocess emits JSON-lines via structlog to stdout (see arbiter/utils/logger.py
        # setup_logging: console_handler on sys.stdout). We look for the shutdown-sequence
        # markers emitted by arbiter.main::run_shutdown_sequence and the final
        # "ARBITER shutdown complete" line.
        shutting_down_markers = [
            "Preparing safety-supervised shutdown",
            "shutting_down",
        ]
        complete_markers = [
            "ARBITER shutdown complete",
            "Stopping all components",
            "phase=complete",
            '"phase": "complete"',
        ]
        phases_seen: set[str] = set()
        raw_lines_with_shutdown = 0

        with open(stdout_path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                if any(m in stripped for m in shutting_down_markers):
                    raw_lines_with_shutdown += 1
                    phases_seen.add("shutting_down")
                if any(m in stripped for m in complete_markers):
                    raw_lines_with_shutdown += 1
                    phases_seen.add("complete")
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                event_str = str(obj.get("event", obj.get("msg", "")))
                event_type = obj.get("type") or obj.get("event")
                # Capture any structlog record whose event/type mentions shutdown.
                if (
                    event_type in ("shutdown_state",)
                    or "shutdown" in str(event_type or "").lower()
                    or "shutdown" in event_str.lower()
                ):
                    shutdown_events_captured.append(obj)
                phase_value = (
                    obj.get("payload", {}).get("phase")
                    if isinstance(obj.get("payload"), dict)
                    else None
                ) or obj.get("phase")
                if phase_value:
                    phases_seen.add(str(phase_value))

        log.info(
            "scenario.shutdown.events_found",
            count=len(shutdown_events_captured),
            phases_seen=sorted(phases_seen),
            raw_marker_hits=raw_lines_with_shutdown,
        )

        # Assertion 1: shutdown phase sequence observed in stdout.
        assert "shutting_down" in phases_seen, (
            f"SAFE-05 INVARIANT: expected 'shutting_down' phase in stdout log; "
            f"got phases={sorted(phases_seen)}, "
            f"sample_events={shutdown_events_captured[:3]}"
        )
        assert "complete" in phases_seen, (
            f"SAFE-05 INVARIANT: expected 'complete' phase in stdout log; "
            f"got phases={sorted(phases_seen)}"
        )

        # Assertion 2: the resting order is CANCELLED on the demo exchange.
        await asyncio.sleep(1.0)  # allow platform to finalize cancel
        remaining = await adapter.list_open_orders_by_client_id(effective_client_order_id)
        assert not remaining, (
            f"SAFE-05 INVARIANT VIOLATED: order {effective_client_order_id} still open on Kalshi "
            f"demo after subprocess SIGINT. cancel_all path did not cancel it. "
            f"remaining={remaining}"
        )

        # Assertion 3: subprocess exited cleanly.
        # Accept 0 (unix clean) or any non-negative (not -9 forced-kill).
        assert return_code is not None and return_code >= 0, (
            f"Subprocess exited with {return_code} — forced kill or crash"
        )

    finally:
        # Ensure log file handles are closed even if assertions raise.
        if not stdout_fh.closed:
            stdout_fh.close()
        if not stderr_fh.closed:
            stderr_fh.close()
        # Ensure the subprocess is terminated if it's still running.
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # Clean up test adapter's aiohttp session. KalshiAdapter has no explicit close;
        # we hold the session locally and close it.
        try:
            if adapter is not None and getattr(adapter, "session", None) is not None:
                sess = adapter.session
                if not sess.closed:
                    await sess.close()
        except Exception:
            pass

    # Write scenario manifest for Plan 04-08 aggregator.
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "sigint_cancels_open_kalshi_demo_orders",
                "requirement_ids": ["SAFE-05", "TEST-01"],
                "phase_3_refs": [
                    "03-05-PLAN",
                    "03-HUMAN-UAT.md Test 2 (partial -- backend only; UI banner reserved)",
                ],
                "tag": "real",
                "subprocess_return_code": return_code,
                # G-2 fix (Plan 04-09): if adapter.place_resting_limit ran, the
                # adapter generated a different client_order_id than the test
                # precomputed. Prefer the effective id; fall back to the
                # test-generated id (used by bypass paths and on early failures).
                "placed_client_order_id": (
                    locals().get("effective_client_order_id") or client_order_id
                ),
                "market": SHUTDOWN_MARKET_TICKER,
                "price": SHUTDOWN_PRICE,
                "qty": SHUTDOWN_QTY,
                "shutdown_events_captured_count": len(shutdown_events_captured),
                "phases_seen": sorted(phases_seen) if "phases_seen" in dir() else [],
                "order_cancelled_on_platform": True,  # reached end without assertion failure
                "platform": sys.platform,
                "non_fok_placement_strategy": non_fok_strategy,
                "shutdown_duration_seconds": (
                    round(shutdown_duration, 3)
                    if shutdown_duration is not None
                    else None
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
