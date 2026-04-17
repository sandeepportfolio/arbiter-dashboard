"""One-leg exposure injected scenario (Scenario 7: SAFE-03 injected).

Fault-injection strategy (per D-11 `injected` tag):
- Polymarket adapter is an AsyncMock whose `place_fok` raises RuntimeError with
  an "INJECTED:" prefix so traceability is unambiguous.
- Rather than drive the full ExecutionEngine recovery loop (Path A), this test
  invokes `supervisor.handle_one_leg_exposure(...)` directly with synthetic
  legs (Path B). Rationale: the supervisor is the component under test for
  SAFE-03 — the engine-side `_recover_one_leg_risk` plumbing is already covered
  by `arbiter/execution/test_engine.py`. Path B exercises the code that fans
  out the Telegram alert + WS event + payload shape, which is what SAFE-03
  promises operators.

The test still requires `sandbox_db_pool` (for evidence dump) and is gated by
`@pytest.mark.live` for consistency with other sandbox scenarios; it DOES NOT
make any real HTTP calls (the injected second leg never hits the wire, so no
funded Polymarket test wallet is consumed).

Analog: arbiter/safety/test_supervisor.py::test_handle_one_leg_exposure_sends_telegram_and_publishes
(lines 143-193) — exact shape we adapt.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog

from arbiter.sandbox import evidence

log = structlog.get_logger("arbiter.sandbox.one_leg")


@pytest.mark.live
async def test_one_leg_recovery_injected(sandbox_db_pool, evidence_dir):
    """First leg (Kalshi) synthetic-fills; second leg (Polymarket) raises (INJECTED);
    SafetySupervisor.handle_one_leg_exposure fires Telegram + WS event + structured payload.
    """
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), (
        "wrong DB — source .env.sandbox before running live"
    )

    # Lazy imports so `pytest --collect-only` works without initialising the
    # full arbiter config chain.
    from arbiter.config.settings import SafetyConfig
    from arbiter.safety.supervisor import SafetySupervisor

    # Mock Polymarket adapter that LOOKS like PolymarketAdapter but raises on place_fok.
    # We cannot use a real PolymarketAdapter because that would require the test wallet + real USDC.
    adapter_poly_mock = AsyncMock()
    adapter_poly_mock.platform = "polymarket"
    adapter_poly_mock.cancel_all = AsyncMock(return_value=[])

    async def injected_raise(*args, **kwargs):
        raise RuntimeError(
            "INJECTED: simulated Polymarket second-leg failure (Scenario 7)"
        )

    adapter_poly_mock.place_fok = injected_raise

    # Mock Kalshi adapter (first leg) — we do NOT place a real order here since
    # the supervisor path is what's being tested. Evidence of Kalshi-adapter
    # behaviour is covered by plans 04-03/04-04.
    adapter_kalshi_mock = AsyncMock()
    adapter_kalshi_mock.platform = "kalshi"
    adapter_kalshi_mock.cancel_all = AsyncMock(return_value=[])

    # Fake notifier for Telegram egress — supervisor wraps in try/except so
    # RuntimeError-raising notifiers would not block the WS publish, but we
    # want to assert the send WAS attempted with the NAKED POSITION payload.
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=None)

    # Real SafetyConfig (plan pseudocode used MagicMock; the supervisor reads
    # config.min_cooldown_seconds during trip_kill — not during one-leg — but
    # using the real dataclass keeps parity with the Phase 3 supervisor test).
    config = SafetyConfig()

    adapters = {"kalshi": adapter_kalshi_mock, "polymarket": adapter_poly_mock}

    # Engine is a dummy SimpleNamespace — handle_one_leg_exposure does not
    # touch self.engine; it only touches self.notifier + self._subscribers.
    supervisor = SafetySupervisor(
        config=config,
        engine=SimpleNamespace(),
        adapters=adapters,
        notifier=notifier,
        redis=None,
        store=None,
        safety_store=None,
    )
    queue = supervisor.subscribe()

    # Build synthetic incident, filled_leg, failed_leg, opp — shape mirrors
    # arbiter/safety/test_supervisor.py::test_handle_one_leg_exposure_sends_telegram_and_publishes
    # so the supervisor sees the same attribute surface it sees in production.
    filled_leg = SimpleNamespace(
        platform="kalshi",
        side="yes",
        fill_qty=100,
        fill_price=0.56,
    )
    failed_leg = SimpleNamespace(
        platform="polymarket",
        side="no",
        error=(
            "INJECTED: simulated Polymarket second-leg failure (Scenario 7) — "
            "rate_limited"
        ),
    )
    incident = SimpleNamespace(
        incident_id="INC-SANDBOX-ONE-LEG",
        metadata={
            "event_type": "one_leg_exposure",
            "filled_platform": "kalshi",
            "filled_side": "yes",
            "filled_qty": 100,
            "filled_price": 0.56,
            "exposure_usd": 56.0,
            "failed_platform": "polymarket",
            "failed_reason": (
                "INJECTED: simulated Polymarket second-leg failure (Scenario 7)"
            ),
            "recommended_unwind": "Sell 100 YES on KALSHI at market",
        },
    )
    opp = SimpleNamespace(canonical_id="MKT1")

    # Invoke the supervisor path directly (Path B).
    await supervisor.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)

    # Assertion 1: Telegram (mock notifier) was called with NAKED POSITION substring.
    notifier.send.assert_awaited()
    sent_messages = [
        call.args[0]
        for call in notifier.send.await_args_list
        if call.args
    ]
    assert any(
        "NAKED POSITION" in msg or "one leg" in msg.lower()
        for msg in sent_messages
    ), f"Expected NAKED POSITION notification; got: {sent_messages}"
    log.info("scenario.one_leg.telegram_sent", messages=sent_messages)

    # Assertion 2: WS event published with one_leg_exposure type.
    try:
        event = queue.get_nowait()
    except asyncio.QueueEmpty:
        pytest.fail(
            "SafetySupervisor did not publish one_leg_exposure event on "
            "handle_one_leg_exposure — check _publish/subscribe wiring"
        )
    assert event.get("type") == "one_leg_exposure", (
        f"Unexpected event type: {event.get('type')}"
    )
    payload = event.get("payload", {})
    # Payload must include canonical_id per supervisor test analog
    # (arbiter/safety/test_supervisor.py:192: assert payload.get("canonical_id") == "MKT1").
    assert payload.get("canonical_id") == "MKT1", (
        f"Expected canonical_id='MKT1' in payload; got payload={payload}"
    )
    log.info("scenario.one_leg.ws_event", event=event)

    # Assertion 3: adapter_poly_mock.place_fok would raise the INJECTED error
    # if invoked (sanity — proves the injection is in place even if this test
    # path does not exercise it).
    with pytest.raises(RuntimeError, match="INJECTED:"):
        await adapter_poly_mock.place_fok(
            arb_id="ARB-TEST", market_id="M1",
            canonical_id="MKT1", side="no", price=0.44, qty=1,
        )

    # Evidence: dump execution tables (empty but proves DB connectivity) and
    # write scenario manifest.
    await evidence.dump_execution_tables(sandbox_db_pool, evidence_dir)
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "one_leg_recovery_injected",
                "requirement_ids": ["SAFE-03", "TEST-01"],
                "tag": "injected",
                "path_taken": "Path B (direct supervisor.handle_one_leg_exposure call)",
                "injection_strategy": (
                    "Polymarket place_fok replaced with async callable raising "
                    "RuntimeError('INJECTED: ...'); first leg synthesised via "
                    "SimpleNamespace(fill_qty=100, fill_price=0.56)."
                ),
                "telegram_sent": bool(notifier.send.await_count),
                "telegram_send_count": notifier.send.await_count,
                "telegram_messages": sent_messages,
                "ws_event_type": event.get("type"),
                "ws_event_payload_canonical_id": payload.get("canonical_id"),
                "ws_event_payload_incident_id": payload.get("incident_id"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
