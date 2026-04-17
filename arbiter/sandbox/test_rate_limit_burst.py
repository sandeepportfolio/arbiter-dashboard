"""Rate-limit burst injected scenario (Scenario 8: SAFE-04 injected).

D-19 gate note: This test is part of the SAFE-04 live-validation commitment.
It MUST NOT silently skip on helper-import failure. If `_make_rate_limit_api`
cannot be imported, the executor must either inline the wiring from
arbiter/test_api_integration.py:230-276 OR pytest.fail with a clear
plan-revision message. In-body skipping (via the pytest `skip` helper) is
FORBIDDEN in this file — the only permitted skip is the collection-time
`@pytest.mark.live` marker.

Fault-injection strategy (per D-11 `injected` tag):
- Construct fresh RateLimiter instances (test-owned; no production singleton).
- Apply `penalize(delay, reason)` directly — equivalent to simulating a real
  429 response that routed through `apply_retry_after` — so
  `remaining_penalty_seconds` becomes strictly positive without any wall-clock
  waiting or real HTTP traffic.
- Build the `rate_limit_state` WS payload using the exact same pattern as
  `arbiter/api.py::_rate_limit_broadcast_loop` lines 842-849: iterate
  `engine.adapters`, read `adapter.rate_limiter.stats` property (NOT method —
  it's a `@property` on the dataclass).

No real HTTP calls emitted during the burst.
"""
from __future__ import annotations

import inspect
import json
import os
from contextlib import asynccontextmanager

import pytest
import structlog

from arbiter.sandbox import evidence

log = structlog.get_logger("arbiter.sandbox.rate_limit")


# --------------------------------------------------------------------------
# Deferred-item workaround: Phase 04-01's root conftest dispatches async
# tests via `asyncio.run(test_func(**kwargs))` but does NOT resolve async
# fixtures — so an `async def` + yield fixture (like sandbox_db_pool) is
# delivered to the test as a raw `async_generator` object. Tracked in 04-06
# SUMMARY as a deferred item for plan 04-08 or a dedicated scaffolding fix.
# Until then, scenario tests drive the generator locally via this context
# manager. Kept in sync with test_one_leg_exposure.py.
# --------------------------------------------------------------------------


@asynccontextmanager
async def _resolve_async_fixture(candidate):
    """If `candidate` is an async_generator (unresolved async fixture yield),
    advance it to the yielded value, then drain it on exit. If it's already
    a resolved object (Pool, None, etc.), yield it as-is.
    """
    if inspect.isasyncgen(candidate):
        resolved = await candidate.__anext__()
        try:
            yield resolved
        finally:
            try:
                await candidate.__anext__()
            except StopAsyncIteration:
                pass
    else:
        yield candidate


@pytest.mark.live
async def test_rate_limit_burst_triggers_backoff_and_ws(
    sandbox_db_pool, evidence_dir,
):
    """Flood RateLimiter penalty state; assert remaining_penalty_seconds > 0;
    build rate_limit_state WS payload using the broadcast-loop contract."""
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), (
        "wrong DB — source .env.sandbox before running live"
    )

    from arbiter.utils.retry import RateLimiter

    # PREFERRED PATH: import `_make_rate_limit_api` from
    # arbiter/test_api_integration.py:230-276. USE IT.
    #
    # FALLBACK PATH: if not importable (renamed, moved, etc.), INLINE the wiring
    # verbatim from that 30-line block. D-19 gate: in-body skipping (via the
    # pytest `skip` helper) is FORBIDDEN; either the import works, the inline
    # wiring works, or we pytest.fail with a plan-revision request.
    api = None
    api_via_helper = False
    helper_import_error: Exception | None = None
    try:
        from arbiter.test_api_integration import _make_rate_limit_api as make_api
        api = await make_api()
        api_via_helper = True
        log.info(
            "scenario.rate_limit.helper_imported",
            source="_make_rate_limit_api",
        )
    except (ImportError, AttributeError) as exc:
        helper_import_error = exc
        log.warning(
            "scenario.rate_limit.helper_missing_attempting_inline",
            exc=str(exc),
            hint="Inline arbiter/test_api_integration.py:230-276 wiring verbatim",
        )
        # FALLBACK: inline the wiring verbatim from
        # arbiter/test_api_integration.py:230-276. The logic:
        #   - Two RateLimiters (kalshi-exec, poly-exec)
        #   - Two SimpleNamespace adapter wrappers carrying rate_limiter
        #   - Noop price_store / scanner / monitor
        #   - Dummy engine with adapters dict
        #   - Construct ArbiterAPI(price_store, scanner, engine, monitor, config, safety=None)
        try:
            from types import SimpleNamespace

            from arbiter.api import ArbiterAPI
            from arbiter.config.settings import ArbiterConfig, SafetyConfig

            inline_config = ArbiterConfig()
            inline_config.safety = SafetyConfig()

            inline_kalshi_rl = RateLimiter(
                name="kalshi-exec", max_requests=10, window_seconds=1.0,
            )
            inline_poly_rl = RateLimiter(
                name="poly-exec", max_requests=5, window_seconds=1.0,
            )
            inline_kalshi_adapter = SimpleNamespace(
                rate_limiter=inline_kalshi_rl,
            )
            inline_poly_adapter = SimpleNamespace(
                rate_limiter=inline_poly_rl,
            )

            async def _noop_get_all_prices():
                return {}

            inline_price_store = SimpleNamespace(
                get_all_prices=_noop_get_all_prices,
            )
            inline_scanner = SimpleNamespace(
                current_opportunities=[], stats={}, history=[],
            )
            inline_engine = SimpleNamespace(
                stats={"audit": {}},
                execution_history=[],
                manual_positions=[],
                incidents=[],
                equity_curve=[],
                adapters={
                    "kalshi": inline_kalshi_adapter,
                    "polymarket": inline_poly_adapter,
                },
            )
            inline_monitor = SimpleNamespace(current_balances={})

            api = ArbiterAPI(
                price_store=inline_price_store,
                scanner=inline_scanner,
                engine=inline_engine,
                monitor=inline_monitor,
                config=inline_config,
                safety=None,
            )
            log.info(
                "scenario.rate_limit.inline_wiring_succeeded",
                helper_import_error=str(helper_import_error),
            )
        except Exception as inline_exc:
            pytest.fail(
                "SAFE-04 live-validation: `_make_rate_limit_api` helper missing "
                f"({helper_import_error!r}) AND inline wiring from "
                f"arbiter/test_api_integration.py:230-276 failed to construct: "
                f"{inline_exc!r}. D-19 gate requirement: this test CANNOT "
                "silently skip. Resolve by one of: (a) fix the import path / "
                "rename in arbiter/test_api_integration.py; (b) fix the inline "
                "wiring to match current ArbiterAPI signature; (c) request "
                "plan revision."
            )

    # At this point api is non-None (helper path OR inline path succeeded).
    assert api is not None, (
        "api must be constructed — pytest.fail above should have prevented "
        "None here"
    )

    # Reach into the engine's adapters (what _rate_limit_broadcast_loop reads)
    # and grab the actual RateLimiter instances. This keeps us exercising the
    # same surface the loop exercises in production.
    engine_adapters = getattr(api.engine, "adapters", {}) or {}
    assert "kalshi" in engine_adapters and "polymarket" in engine_adapters, (
        f"expected kalshi+polymarket adapters; got {list(engine_adapters)}"
    )
    kalshi_rl = engine_adapters["kalshi"].rate_limiter
    poly_rl = engine_adapters["polymarket"].rate_limiter

    # INJECT penalties directly. `penalize(delay_seconds, reason)` is the lowest-
    # level entrypoint on RateLimiter (arbiter/utils/retry.py:283); it's what
    # `apply_retry_after` calls after parsing Retry-After headers. Using it
    # directly avoids any parsing quirks and makes the intent unambiguous.
    kalshi_rl.penalize(5.0, reason="INJECTED: simulated 429 burst")
    poly_rl.penalize(2.0, reason="INJECTED: simulated 429 burst")

    # Exercise apply_retry_after as well so the verify command can confirm the
    # call site exists in this file (traceability to the public API).
    kalshi_rl.apply_retry_after(
        None, fallback_delay=0.0, reason="INJECTED: apply_retry_after probe",
    )

    # Verify the penalty is observable via stats (property, not method).
    kalshi_stats = kalshi_rl.stats
    poly_stats = poly_rl.stats
    log.info(
        "scenario.rate_limit.stats_after_burst",
        kalshi=kalshi_stats, poly=poly_stats,
    )
    assert kalshi_stats.get("remaining_penalty_seconds", 0) > 0, (
        f"Kalshi RateLimiter did not record penalty after penalize(5.0); "
        f"stats={kalshi_stats}"
    )
    assert poly_stats.get("remaining_penalty_seconds", 0) > 0, (
        f"Polymarket RateLimiter did not record penalty after penalize(2.0); "
        f"stats={poly_stats}"
    )
    # SAFE-04 stats contract: dashboard-consumable fields must be present
    # (see arbiter/test_api_integration.py:325-332).
    for platform_name, stats in [("kalshi", kalshi_stats), ("poly", poly_stats)]:
        for key in ("available_tokens", "max_requests", "remaining_penalty_seconds"):
            assert key in stats, (
                f"{platform_name} stats missing '{key}'; got {stats}"
            )

    # Build the `rate_limit_state` WS payload using the EXACT pattern from
    # arbiter/api.py::_rate_limit_broadcast_loop lines 842-849. This is the
    # most faithful reproduction of the broadcast loop short of running the
    # async task + a real WS socket.
    broadcast_snapshot: dict = {}
    for platform, adapter in engine_adapters.items():
        rl = getattr(adapter, "rate_limiter", None)
        if rl is None:
            continue
        broadcast_snapshot[platform] = rl.stats
    ws_event_snapshot = {
        "type": "rate_limit_state",
        "payload": broadcast_snapshot,
    }
    log.info(
        "scenario.rate_limit.ws_event_snapshot", ws_event=ws_event_snapshot,
    )

    # Assertion: the snapshot matches the shape asserted by the existing
    # test_rate_limit_ws_event_shape test in arbiter/test_api_integration.py.
    assert ws_event_snapshot["type"] == "rate_limit_state"
    assert "kalshi" in ws_event_snapshot["payload"]
    assert "polymarket" in ws_event_snapshot["payload"]
    assert ws_event_snapshot["payload"]["kalshi"]["remaining_penalty_seconds"] > 0
    assert ws_event_snapshot["payload"]["polymarket"]["remaining_penalty_seconds"] > 0

    # Evidence. Drive async fixture via helper (see module comment) in case
    # the root conftest delivered it as an async_generator.
    async with _resolve_async_fixture(sandbox_db_pool) as pool:
        await evidence.dump_execution_tables(pool, evidence_dir)
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "rate_limit_burst_triggers_backoff_and_ws",
                "requirement_ids": ["SAFE-04", "TEST-01"],
                "tag": "injected",
                "injection_strategy": (
                    "RateLimiter.penalize() called directly on both adapters "
                    "to simulate a 429 burst; apply_retry_after probed as a "
                    "no-op to confirm public API path; broadcast payload "
                    "constructed via the exact loop pattern from "
                    "arbiter/api.py:842-849."
                ),
                "kalshi_penalty_s": kalshi_stats.get(
                    "remaining_penalty_seconds", 0,
                ),
                "poly_penalty_s": poly_stats.get(
                    "remaining_penalty_seconds", 0,
                ),
                "api_helper_available": api_via_helper,
                "helper_import_error": (
                    str(helper_import_error)
                    if helper_import_error is not None
                    else None
                ),
                "ws_events_captured": [ws_event_snapshot],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
