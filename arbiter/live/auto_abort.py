"""Phase 5 auto-abort primitive — wire reconcile_post_trade to SafetySupervisor.trip_kill.

Fail-closed: if reconcile itself raises, the kill switch STILL trips. Per
Plan 05-02 research §Security Domain "Reconciliation step itself fails silently
-> default to abort (fail-closed)." Silent failure of reconcile is a worse
outcome than a false-positive kill-switch arming: the operator can always RESET
the kill after verifying nothing is actually broken, but a missed reconcile
breach means a real fee discrepancy (or PnL drift) goes undetected on the
first-ever live trade.

T-5-02-01 (tampering): bogus empty list from reconcile_fn bypasses this wrapper.
The defense is to require reconcile_post_trade to use a real adapter-backed
fee_fetcher (see arbiter.live.live_fire_helpers) — the wrapper trusts what
reconcile_post_trade returns and only adds the wire-up to trip_kill. Ground-truth
checking is reconcile's job, not this wrapper's.

T-5-02-02 (DoS via repeated trip_kill): this wrapper has NO de-dup. The real
SafetySupervisor.trip_kill is internally idempotent (serialized by
``self._state_lock``; already-armed returns current state without re-cancelling).
Double-invocation of wire_auto_abort_on_reconcile produces two trip_kill calls
but only one actually fires side effects — by design.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog

log = structlog.get_logger("arbiter.live.auto_abort")


TRIP_ACTOR = "system:phase5_reconcile_fail"


async def wire_auto_abort_on_reconcile(
    supervisor,
    reconcile_fn: Callable[[], Awaitable[Optional[List[Dict[str, Any]]]]],
) -> Dict[str, Any]:
    """Run reconcile; on breach OR exception, trip the kill switch.

    Args:
        supervisor: ``SafetySupervisor`` with an async ``trip_kill(by, reason)``.
        reconcile_fn: zero-arg async callable returning a list of discrepancy
            dicts (empty list = clean) or None. Typically a partial that closes
            over ``reconcile_post_trade(execution, adapters, fee_fetcher=...)``.

    Returns:
        Dict with:
          - ``aborted`` (bool): True iff trip_kill was attempted.
          - ``discrepancies`` (list): what reconcile returned (empty on clean
            or on exception).
          - ``error`` (str | None): present only when reconcile raised;
            carries the exception's string form for the caller to log.
    """
    # Branch 1: reconcile itself raises -> fail-closed trip (T-5-02-01 safe
    # default). Per research, a reconcile exception is worse than an unnecessary
    # kill: the operator can reset after verifying, but a missed breach is
    # undetectable without replay.
    try:
        discrepancies = await reconcile_fn()
    except Exception as exc:
        err_str = str(exc)
        log.error(
            "phase5.reconcile.exception",
            err=err_str,
            err_type=type(exc).__name__,
        )
        reason = f"reconcile_error: {type(exc).__name__}: {err_str}"
        try:
            await supervisor.trip_kill(by=TRIP_ACTOR, reason=reason)
        except Exception as trip_exc:
            # This is the true worst case — reconcile failed AND trip_kill
            # failed. Log with critical severity; nothing else we can do here.
            # The operator MUST notice this via dashboard / Telegram outages.
            log.critical(
                "phase5.trip_kill.failed_after_reconcile_exception",
                err=str(trip_exc),
                original_err=err_str,
            )
        return {"aborted": True, "discrepancies": [], "error": err_str}

    # Branch 2: reconcile returned None (defensive — shouldn't happen but
    # treat as clean so a bug in reconcile does not spuriously kill).
    if discrepancies is None:
        log.info("phase5.reconcile.returned_none", note="treating as clean")
        return {"aborted": False, "discrepancies": []}

    # Branch 3: reconcile returned an empty list -> clean, no trip.
    if not discrepancies:
        log.info("phase5.reconcile.ok")
        return {"aborted": False, "discrepancies": []}

    # Branch 4: reconcile returned a non-empty discrepancy list -> breach,
    # trip the kill switch. by='system:phase5_reconcile_fail' so dashboard
    # filters can distinguish auto-aborts from operator arms.
    reason = _format_reason(discrepancies)
    log.warning(
        "phase5.reconcile.breach",
        discrepancy_count=len(discrepancies),
        first_reason=discrepancies[0].get("reason") if isinstance(discrepancies[0], dict) else None,
    )
    try:
        await supervisor.trip_kill(by=TRIP_ACTOR, reason=reason)
    except Exception as trip_exc:
        # Same failure mode as branch 1's inner except — log critical, return
        # aborted=True because from the caller's perspective the abort was
        # attempted. The supervisor's own error recovery is the final belt.
        log.critical(
            "phase5.trip_kill.failed_after_reconcile_breach",
            err=str(trip_exc),
            reason=reason,
        )
    return {"aborted": True, "discrepancies": list(discrepancies), "error": None}


def _format_reason(discrepancies: List[Dict[str, Any]]) -> str:
    """Build a kill-switch reason string summarizing each breach.

    Caps at 3 discrepancies for readability — SafetySupervisor.trip_kill logs
    the full dict on the Postgres safety_events row anyway, so the reason is
    for human-readable Telegram + dashboard banner, not the canonical record.
    """
    parts: List[str] = []
    for d in discrepancies[:3]:
        if not isinstance(d, dict):
            parts.append(repr(d))
            continue
        kind = d.get("reason", "unknown")
        amt_raw = d.get("discrepancy", 0.0)
        try:
            amt_fmt = f"{float(amt_raw):+.4f}"
        except (TypeError, ValueError):
            amt_fmt = str(amt_raw)
        plat = d.get("platform", "?")
        parts.append(f"{plat}:{kind}={amt_fmt}")
    if len(discrepancies) > 3:
        parts.append(f"(+{len(discrepancies) - 3} more)")
    return "phase5_reconcile_fail: " + "; ".join(parts)
