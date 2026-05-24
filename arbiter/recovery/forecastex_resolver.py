"""ForecastEx child-conid resolver — periodic auto-heal loop.

The 24 confirmed mappings with ``forecastex_contract_id`` set are
currently parent EVENT conids (assetClass=IND, secType=EC). Parent
conids are NOT tradeable — IBKR's gateway returns empty bid/ask
snapshots and the collector marks them inactive after 3 probes.

The tradeable contracts are CHILD conids under each event (binary
options). ``ForecastExClient.resolve_event_children`` tries multiple
IBKR endpoints (``/iserver/secdef/info`` with sectype=EC + month,
``/iserver/secdef/search``, etc.) to enumerate them.

IBKR's FORECASTX EC endpoints are flaky in practice:
  - 400 Bad Request when month is missing
  - 503 Service Unavailable on weekends
  - 503 / empty list when the contract month string is wrong

So a one-shot resolve at discovery time is brittle. We need a periodic
retry that:
  1. Walks all confirmed mappings whose ``forecastex_contract_id`` is
     marked inactive by the collector (parent that doesn't trade).
  2. Calls ``resolve_event_children`` for each.
  3. If a YES child conid comes back, persists it to the DB via
     ``mapping_store.upsert`` (which propagates to MARKET_MAP via
     ``upsert_runtime_market_mapping``).
  4. Reactivates the new child conid in the collector so the next
     poll cycle hits a tradeable contract.
  5. Surfaces resolver-state for the ops UI: cycle count, last attempt
     timestamp, last-attempt outcome per canonical_id.

Default cadence is 30 minutes; tunable via FX_CHILD_RESOLVE_INTERVAL_S.
The resolver auto-disables itself (returns early) if no
``forecastex_client`` / ``mapping_store`` / ``collector`` is wired —
unit tests and dev mode are unaffected.

This service is OBSERVE-ONLY when ``FX_RESOLVER_DRY_RUN=true`` —
useful for ops to verify a resolver pass without actually mutating
the DB.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import structlog

logger = structlog.get_logger("arbiter.recovery.forecastex_resolver")


@dataclass
class ResolveAttempt:
    """One per-mapping resolution attempt result, for the ops UI."""

    canonical_id: str
    parent_conid: str
    ts: float
    outcome: str  # "resolved" | "no_children" | "ibkr_503" | "ibkr_400" | "exception" | "dry_run"
    child_conid: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "parent_conid": self.parent_conid,
            "ts": self.ts,
            "age_seconds": round(time.time() - self.ts, 1),
            "outcome": self.outcome,
            "child_conid": self.child_conid,
            "detail": self.detail[:200],
        }


@dataclass
class ResolverSnapshot:
    """Result of one resolver cycle — exposed via /api/system."""

    timestamp: float
    cycle_count: int
    candidates_count: int
    resolved_count: int
    failed_count: int
    duration_ms: float
    attempts: List[Dict[str, Any]]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cycle_count": self.cycle_count,
            "candidates_count": self.candidates_count,
            "resolved_count": self.resolved_count,
            "failed_count": self.failed_count,
            "duration_ms": round(self.duration_ms, 1),
            "attempts": self.attempts,
            "dry_run": self.dry_run,
        }


class ForecastExChildResolver:
    """Background service that resolves parent → child FORECASTX conids."""

    def __init__(
        self,
        *,
        forecastex_client: Any,
        forecastex_collector: Any,
        mapping_store: Any,
        interval_s: float = 1800.0,
        dry_run: bool = False,
    ) -> None:
        self._client = forecastex_client
        self._collector = forecastex_collector
        self._mapping_store = mapping_store
        self._interval_s = max(60.0, float(interval_s))
        self._dry_run = bool(dry_run)
        self._cycle_count: int = 0
        # Per-canonical-id last attempt (for ops UI).
        self._last_attempts: Dict[str, ResolveAttempt] = {}
        self._last_snapshot: Optional[ResolverSnapshot] = None
        self._stopped: bool = False
        self._task: Optional[asyncio.Task] = None

    @property
    def last_snapshot(self) -> Optional[ResolverSnapshot]:
        return self._last_snapshot

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_loop(self) -> None:
        # Initial sleep so the resolver doesn't compete with collector
        # cold-start for the IBKR rate-limit budget.
        await asyncio.sleep(60.0)
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("forecastex_resolver.cycle_failed", err=str(exc))
            await asyncio.sleep(self._interval_s)

    # ─── single-cycle entry point (also used by tests + manual API) ───────

    def _candidate_canonical_ids(self) -> List[tuple]:
        """Return mappings where the FX conid exists but the collector
        has it disabled (parent not tradeable) — those are the only
        resolution candidates worth probing each cycle.
        """
        from arbiter.config.settings import MARKET_MAP

        inactive = set(getattr(self._collector, "_inactive_conids", set()) or set())
        out: List[tuple] = []
        for canonical_id, mapping in MARKET_MAP.items():
            if mapping.get("status") != "confirmed":
                continue
            conid = str(mapping.get("forecastex") or "").strip()
            if not conid:
                continue
            if conid not in inactive:
                continue
            out.append((canonical_id, conid))
        return out

    async def run_once(self) -> ResolverSnapshot:
        t0 = time.monotonic()
        self._cycle_count += 1
        candidates = self._candidate_canonical_ids()
        attempts: List[ResolveAttempt] = []
        resolved = failed = 0
        for canonical_id, parent_conid in candidates:
            attempt = await self._resolve_one(canonical_id, parent_conid)
            attempts.append(attempt)
            self._last_attempts[canonical_id] = attempt
            if attempt.outcome == "resolved":
                resolved += 1
            elif attempt.outcome != "dry_run":
                failed += 1

        duration_ms = (time.monotonic() - t0) * 1000.0
        snap = ResolverSnapshot(
            timestamp=time.time(),
            cycle_count=self._cycle_count,
            candidates_count=len(candidates),
            resolved_count=resolved,
            failed_count=failed,
            duration_ms=duration_ms,
            attempts=[a.to_dict() for a in attempts],
            dry_run=self._dry_run,
        )
        self._last_snapshot = snap
        logger.info(
            "forecastex_resolver.cycle_complete",
            cycle=self._cycle_count,
            candidates=len(candidates),
            resolved=resolved,
            failed=failed,
            duration_ms=round(duration_ms, 1),
        )
        return snap

    async def _resolve_one(
        self, canonical_id: str, parent_conid: str,
    ) -> ResolveAttempt:
        now = time.time()
        try:
            children = await self._client.resolve_event_children(parent_conid)
        except Exception as exc:
            # The client wraps IBKR errors as ClientResponseError; check
            # status for the most actionable bucket.
            status = getattr(exc, "status", None)
            outcome = (
                "ibkr_503" if status == 503
                else "ibkr_400" if status == 400
                else "exception"
            )
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome=outcome, detail=str(exc)[:200],
            )

        if not children:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="no_children",
                detail="resolve_event_children returned empty list (IBKR endpoint may be down or contract has no child option chain)",
            )

        # Pick the YES side. Convention from forecastex_discovery: right
        # in {"Y","C","1","YES"} → YES; otherwise first child.
        yes_child = next(
            (c for c in children
             if str(c.get("right", "")).upper() in ("Y", "C", "1", "YES")),
            children[0],
        )
        child_conid = str(yes_child.get("conid") or "").strip()
        if not child_conid:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="no_children",
                detail="children returned without a conid field",
            )

        if self._dry_run:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="dry_run", child_conid=child_conid,
                detail=f"would have attached child conid {child_conid}",
            )

        # Persist the child conid via the mapping store. The
        # mapping_store.upsert path triggers upsert_runtime_market_mapping
        # which mutates the in-process MARKET_MAP — so the next collector
        # cycle picks up the new conid.
        try:
            mapping = await self._mapping_store.get(canonical_id)
            if mapping is None:
                return ResolveAttempt(
                    canonical_id=canonical_id, parent_conid=parent_conid,
                    ts=now, outcome="exception",
                    detail="mapping disappeared between candidate selection and update",
                )
            mapping.forecastex_contract_id = child_conid
            await self._mapping_store.upsert(mapping)
        except Exception as exc:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="exception",
                detail=f"mapping_store.upsert failed: {exc}",
            )

        # Tell the collector the OLD parent is no longer reserved so any
        # future remap is unobstructed; the NEW child will be fresh too.
        try:
            self._collector.reactivate_conid(parent_conid)
            self._collector.reactivate_conid(child_conid)
        except Exception:
            pass

        logger.info(
            "forecastex_resolver.attached",
            canonical_id=canonical_id,
            parent_conid=parent_conid,
            child_conid=child_conid,
            right=yes_child.get("right"),
            source=yes_child.get("source"),
        )
        return ResolveAttempt(
            canonical_id=canonical_id, parent_conid=parent_conid,
            ts=now, outcome="resolved", child_conid=child_conid,
            detail=f"attached child conid {child_conid} (right={yes_child.get('right')})",
        )


def resolver_from_env(
    *, forecastex_client, forecastex_collector, mapping_store,
) -> Optional[ForecastExChildResolver]:
    """Build a resolver with env-driven settings. Returns None if any
    required wiring is missing — caller can simply not start the task.

    Env vars:
      - FX_CHILD_RESOLVE_INTERVAL_S  (default 1800 = 30min)
      - FX_RESOLVER_DRY_RUN          (default false)
    """
    if forecastex_client is None or forecastex_collector is None or mapping_store is None:
        return None
    try:
        interval = float(
            os.getenv("FX_CHILD_RESOLVE_INTERVAL_S", "1800") or "1800"
        )
    except (TypeError, ValueError):
        interval = 1800.0
    dry_run = str(os.getenv("FX_RESOLVER_DRY_RUN", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )
    return ForecastExChildResolver(
        forecastex_client=forecastex_client,
        forecastex_collector=forecastex_collector,
        mapping_store=mapping_store,
        interval_s=interval,
        dry_run=dry_run,
    )
