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
        # Event-driven wakeup. The collector (or any other caller) sets
        # this when a new candidate appears — e.g. after a parent conid
        # exceeds the probe-disable threshold and lands in
        # ``_inactive_conids``. Without it, the loop only runs once per
        # ``interval_s`` (default 30 min) and the first cycle after a
        # container restart almost always races ahead of the collector's
        # initial disable batch with an empty inactive set.
        self._wake_event: asyncio.Event = asyncio.Event()
        # Lifetime cache of successful resolve_event_children lookups,
        # keyed by parent_conid. Avoids re-querying /iserver/secdef/
        # strikes (which IBKR rate-limits tightly — verified 2026-05-26
        # that every one of 41 candidates 429'd in a single cycle when
        # we re-asked for the same data we'd already resolved). Empty
        # results are NOT cached so a temporary 503/429 doesn't poison
        # future cycles.
        self._strikes_cache: Dict[str, List[Dict[str, Any]]] = {}
        # Monotonic timestamp of the last actual IBKR strikes call —
        # used to pace successive calls so we stay under the rate limit.
        self._last_strikes_call_t: float = 0.0
        # Minimum gap between successive strikes calls, in seconds.
        # IBKR's bucket on /iserver/secdef/strikes refills slow enough
        # that back-to-back calls 429 immediately.
        self._strikes_min_gap_s: float = 3.0
        # Per-parent counter of consecutive ``no_children`` outcomes.
        # When a parent stays empty for ``_NO_CHILDREN_MARK_THRESHOLD``
        # cycles in a row, we mark every mapping bound to it
        # ``forecastex_not_available`` so the candidate filter stops
        # re-probing it next cycle. Catches wrong-domain discovery
        # attachments (e.g. sports mappings auto-matched to regional
        # CPI events whose secdef/info 500s on every strike) that
        # would otherwise burn IBKR rate-limit budget forever.
        self._no_children_counts: Dict[str, int] = {}

    # Three failed cycles before declaring a parent permanently
    # unresolvable. Lower than that and a single weekend IBKR outage
    # would wrongly disable healthy events; higher and the 39 bad
    # MLB→CPI attachments would clog the candidate set for days.
    _NO_CHILDREN_MARK_THRESHOLD: int = 3

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

    async def _resolve_event_children_cached(
        self, parent_conid: str,
    ) -> List[Dict[str, Any]]:
        """Cached + paced wrapper around ``client.resolve_event_children``.

        Two protections against IBKR's tight strikes-endpoint rate limit:

        1. Lifetime cache of successful (non-empty) lookups. Two
           confirmed mappings that share an underlying parent conid
           (e.g. DEM_HOUSE and GOP_HOUSE on the HORC event) only
           ever cost ONE strikes call, not two. The cache persists
           across resolver cycles so steady-state re-runs are free.
        2. Pacing — we ensure at least ``_strikes_min_gap_s`` seconds
           elapse between consecutive strikes calls across ALL
           candidates in a cycle. The previous behavior burst-queried
           every candidate as fast as possible, which 429'd 41/41 in
           prod 2026-05-26.

        Empty results are NOT cached: a transient 503 or fully-429'd
        cycle would otherwise poison the cache forever.
        """
        cached = self._strikes_cache.get(parent_conid)
        if cached:
            return list(cached)
        elapsed = time.monotonic() - self._last_strikes_call_t
        if elapsed < self._strikes_min_gap_s:
            await asyncio.sleep(self._strikes_min_gap_s - elapsed)
        self._last_strikes_call_t = time.monotonic()
        children = await self._client.resolve_event_children(parent_conid)
        if children:
            self._strikes_cache[parent_conid] = list(children)
        return children

    def trigger(self) -> None:
        """Wake the resolver loop early.

        Typically wired into the collector so that as soon as a parent
        conid hits the probe-disable threshold and joins the inactive
        set, the resolver runs a cycle within seconds rather than
        waiting up to ``interval_s`` for the next periodic tick.
        Idempotent — a burst of rapid-fire calls coalesces into one
        cycle via the debounce in ``_wait_for_trigger_or_timeout``.
        """
        self._wake_event.set()

    async def _wait_for_trigger_or_timeout(self, timeout: float) -> None:
        """Sleep up to ``timeout`` seconds, returning early when
        ``trigger()`` fires. Triggered wakeups are followed by a 60s
        debounce so a burst of disables (e.g. the collector flushing
        ~30 untradeable parent conids in the first few minutes after
        startup) coalesces into a single cycle rather than burning
        through the IBKR rate-limit budget."""
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        # Clear BEFORE the debounce so any triggers fired during the
        # debounce window register as a new wake signal for the next
        # iteration (rather than being silently swallowed).
        self._wake_event.clear()
        logger.info("forecastex_resolver.triggered_wake")
        await asyncio.sleep(60.0)

    async def _run_loop(self) -> None:
        logger.info(
            "forecastex_resolver.loop_started",
            interval_s=self._interval_s, dry_run=self._dry_run,
        )
        # First iteration uses the same wait-or-trigger primitive so
        # cycle 1 doesn't race ahead of the collector. Verified live
        # 2026-05-26: with a fixed 60s startup sleep, cycle 1 fired
        # against an empty _inactive_conids set every restart and the
        # next cycle was 30 min away — leaving 40 of 43 markets
        # quote-less. Triggered wakeup fixes this: the collector calls
        # trigger() as soon as it disables its first parent.
        while not self._stopped:
            await self._wait_for_trigger_or_timeout(self._interval_s)
            if self._stopped:
                break
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("forecastex_resolver.cycle_failed", err=str(exc))

    # ─── single-cycle entry point (also used by tests + manual API) ───────

    def _candidate_canonical_ids(self) -> List[tuple]:
        """Return mappings where the FX conid exists but the collector
        has it disabled (parent not tradeable) — those are the only
        resolution candidates worth probing each cycle.

        Skips mappings flagged ``forecastex_not_available=true``
        because the resolver already determined no tradeable child
        exists for that parent (e.g. the wrong-product-class MLB→CPI
        mismatches surfaced 2026-05-25). Re-probing those every cycle
        would burn IBKR's rate-limit budget and loop indefinitely.
        """
        from arbiter.config.settings import MARKET_MAP

        inactive = set(getattr(self._collector, "_inactive_conids", set()) or set())
        out: List[tuple] = []
        for canonical_id, mapping in MARKET_MAP.items():
            if mapping.get("status") != "confirmed":
                continue
            if bool(mapping.get("forecastex_not_available")):
                continue
            conid = str(mapping.get("forecastex") or "").strip()
            if not conid:
                continue
            if conid not in inactive:
                continue
            out.append((canonical_id, conid))
        return out

    async def _quarantine_wrong_domain_candidates(
        self, candidates: List[tuple],
    ) -> List[tuple]:
        """Drop sports-tagged mappings from the candidate list and mark
        them ``forecastex_not_available`` so they stop showing up.

        FORECASTX's listed inventory is overwhelmingly political and
        macro-economic; sports inventory has been promised but never
        materialised through 2026-05. Despite that, 39 confirmed Kalshi
        sports mappings (MLB games, NHL Hart trophy lots, etc.) were
        auto-bound to FORECASTX regional-CPI parents on 2026-05-25
        because they shared city-name tokens. Each cycle, those 39
        parents return no_children, hit the per-parent 3-empty mark,
        and then get cleared — but only after burning ~3 strikes calls
        per parent against IBKR's tight rate limit.

        This pre-filter looks at the MAPPING (not the conid) and
        recognises sports-tagged ones up front: their canonical_id /
        description contain explicit sports tokens (mlb / nfl / nba /
        etc.). We mark them unavailable immediately and skip them. Net
        effect: ~120 wasted IBKR calls per cycle disappear, healthy
        political parents get priority on the rate budget, and the
        mapping is preserved (the operator can still attach a known-
        good conid via the manual-override endpoint).
        """
        from arbiter.config.settings import MARKET_MAP
        from arbiter.mapping.forecastex_discovery import _SPORTS_HINT_TOKENS, _tokenize

        kept: List[tuple] = []
        quarantined: List[str] = []
        for canonical_id, parent_conid in candidates:
            mapping = MARKET_MAP.get(canonical_id) or {}
            text = " ".join(str(v) for v in (
                canonical_id,
                mapping.get("description") or "",
                mapping.get("polymarket_question") or "",
                mapping.get("kalshi") or "",
                mapping.get("kalshi_market_id") or "",
            ) if v)
            tokens = _tokenize(text)
            if tokens & _SPORTS_HINT_TOKENS:
                quarantined.append(canonical_id)
                # Persist the unavailable flag so the candidate filter
                # immediately stops re-selecting it next cycle. Use the
                # existing _mark_unavailable path so the resolver state
                # (DB + collector inactive set) stays consistent.
                try:
                    await self._mark_unavailable(
                        canonical_id, parent_conid,
                        reason=(
                            "sports-tagged mapping with FORECASTX conid — "
                            "FORECASTX has no live sports inventory; "
                            "wrong-domain auto-bind from discovery 2026-05-25"
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "forecastex_resolver.quarantine_failed",
                        canonical_id=canonical_id, err=str(exc),
                    )
                continue
            kept.append((canonical_id, parent_conid))
        if quarantined:
            logger.info(
                "forecastex_resolver.quarantined_wrong_domain",
                count=len(quarantined),
                canonical_ids=quarantined[:10],
            )
        return kept

    async def run_once(self) -> ResolverSnapshot:
        t0 = time.monotonic()
        self._cycle_count += 1
        candidates = self._candidate_canonical_ids()
        # Pre-flight: drop sports-tagged mappings before any IBKR call
        # so we don't burn rate budget on the 39 known wrong-domain
        # attachments from the 2026-05-25 incident.
        candidates = await self._quarantine_wrong_domain_candidates(candidates)
        # Sort by parent_conid so candidates sharing the same underlying
        # are processed back-to-back. The first one pays the strikes
        # call; the rest hit ``_strikes_cache`` for free. Without this
        # adjacency, the cache still wins, but pacing waits could fire
        # between two cache-hits that didn't actually call IBKR.
        candidates = sorted(candidates, key=lambda pair: pair[1])
        unique_parents = len({c for _, c in candidates})
        logger.info(
            "forecastex_resolver.cycle_start",
            cycle=self._cycle_count, candidates=len(candidates),
            unique_parents=unique_parents,
            cached_parents=len(self._strikes_cache),
        )
        attempts: List[ResolveAttempt] = []
        resolved = failed = 0
        consecutive_failures = 0
        for canonical_id, parent_conid in candidates:
            # Short-circuit: if we've hit 3 consecutive IBKR failures
            # (503s, exceptions, no_children — anything non-resolved),
            # the /iserver/secdef/* endpoint is effectively down right
            # now. resolve_event_children probes ~10 endpoints per
            # conid; with the FX circuit breaker tripping after 5
            # consecutive failures, ALL subsequent calls in the same
            # cycle raise RuntimeError("circuit OPEN") which classifies
            # as "exception". 24 conids × 30s circuit-recovery wait =
            # 12 minutes blocked on a futile cycle. Once we know the
            # endpoint isn't responding, mark the rest as ibkr_503 and
            # pick up next interval (when the circuit will be closed
            # again).
            if consecutive_failures >= 3:
                attempts.append(ResolveAttempt(
                    canonical_id=canonical_id, parent_conid=parent_conid,
                    ts=time.time(), outcome="ibkr_503",
                    detail="short-circuited after 3 consecutive IBKR failures — endpoint down or circuit open",
                ))
                self._last_attempts[canonical_id] = attempts[-1]
                failed += 1
                continue
            attempt = await self._resolve_one(canonical_id, parent_conid)
            attempts.append(attempt)
            self._last_attempts[canonical_id] = attempt
            if attempt.outcome == "resolved":
                resolved += 1
                consecutive_failures = 0
            elif attempt.outcome == "reactivated_child":
                # Conid was already a tradeable child — the collector
                # had it disabled by mistake.  Counts as a successful
                # heal for the cycle's tally and a healthy-IBKR signal.
                resolved += 1
                consecutive_failures = 0
            elif attempt.outcome == "dry_run":
                # Dry-run reached the child, IBKR was healthy — reset.
                consecutive_failures = 0
            elif attempt.outcome == "marked_unavailable":
                # IBKR responded successfully; we just determined the
                # parent has no tradeable binary child for our purpose.
                # That's a clean termination state, not a failure cluster.
                consecutive_failures = 0
            else:
                consecutive_failures += 1
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

        # Self-heal step — before treating ``parent_conid`` as an event
        # parent and probing its child option chain, snapshot the conid
        # itself.  If IBKR returns a tradeable bid/ask, the conid is
        # ALREADY a working child that was wrongly disabled by the
        # collector (e.g. during an IBKR snapshot warmup race — see
        # arbiter.collectors.forecastex._snapshot_with_warmup).  In that
        # case we reactivate the conid in the collector and return
        # ``reactivated_child`` rather than wasting a full
        # ``resolve_event_children`` cycle on it, which would always
        # fail because a child OPT conid has no further option chain.
        try:
            from arbiter.collectors.forecastex import ForecastExClient as _FxClient
            warmup_snap: Dict[str, Any] = {}
            for warmup_no in (1, 2, 3):
                warmup_snap = await self._client.market_snapshot(parent_conid)
                if _FxClient.is_tradeable_snapshot(warmup_snap):
                    break
                await asyncio.sleep(1.0)
            if _FxClient.is_tradeable_snapshot(warmup_snap):
                try:
                    self._collector.reactivate_conid(parent_conid)
                except Exception:
                    pass
                logger.info(
                    "forecastex_resolver.reactivated_child",
                    canonical_id=canonical_id,
                    conid=parent_conid,
                )
                return ResolveAttempt(
                    canonical_id=canonical_id, parent_conid=parent_conid,
                    ts=now, outcome="reactivated_child",
                    child_conid=parent_conid,
                    detail=(
                        f"conid {parent_conid} is already a tradeable child; "
                        f"reactivated in collector without re-resolving"
                    ),
                )
        except Exception as exc:
            # Best-effort warmup probe — fall through to the parent
            # resolution flow on any failure so we don't lose the
            # existing ibkr_503/ibkr_400 outcome classification below.
            logger.debug(
                "forecastex_resolver.warmup_snapshot_failed",
                canonical_id=canonical_id, err=str(exc),
            )

        try:
            children = await self._resolve_event_children_cached(parent_conid)
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
            count = self._no_children_counts.get(parent_conid, 0) + 1
            self._no_children_counts[parent_conid] = count
            if count >= self._NO_CHILDREN_MARK_THRESHOLD:
                # Persistent emptiness — either IBKR genuinely doesn't
                # expose children for this conid (wrong-domain discovery
                # attach, e.g. MLB game mapping bound to a regional CPI
                # event) or the secdef endpoint is permanently broken
                # for it. Either way, marking the mapping unavailable
                # stops it from being a candidate so future cycles
                # don't waste budget retrying.
                self._no_children_counts.pop(parent_conid, None)
                return await self._mark_unavailable(
                    canonical_id, parent_conid,
                    reason=(
                        f"resolve_event_children empty after {count} "
                        f"consecutive cycles — likely wrong-domain "
                        f"discovery attach or unsupported IBKR conid"
                    ),
                )
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="no_children",
                detail=(
                    f"resolve_event_children returned empty list "
                    f"(attempt {count}/{self._NO_CHILDREN_MARK_THRESHOLD} "
                    f"before mark_unavailable)"
                ),
            )

        # Children resolved — reset the per-parent no_children counter
        # so a transient empty cycle doesn't accumulate toward the
        # mark-unavailable threshold for a healthy parent.
        self._no_children_counts.pop(parent_conid, None)

        # Pick the YES side — IBKR right=C is the Call which pays $1 when
        # the strike outcome resolves true (e.g. HORC strike=1 Call =
        # "Dems win House"). For canonical_ids that encode a party hint
        # (DEM_*, GOP_*, etc.) prefer the matching strike.
        yes_child = self._select_yes_child(canonical_id, children)
        from arbiter.collectors.forecastex import is_valid_conid

        child_conid = str(yes_child.get("conid") or "").strip() if yes_child else ""
        if not is_valid_conid(child_conid):
            # No Call-right entry in the returned set; mark not-available
            # so the operator can override manually.
            return await self._mark_unavailable(
                canonical_id, parent_conid,
                reason="no Call (YES) child returned",
            )

        # 2026-06-02 wrong-domain blocklist gate. The discovery and
        # resolver paths both attach conids; the blocklist guarantees no
        # historically-bad conid re-binds even if a future scoring change
        # would let it slip through.
        from arbiter.mapping.forecastex_discovery import (
            is_blocklisted_forecastex_conid as _is_blk,
        )
        if _is_blk(child_conid) or _is_blk(parent_conid):
            return await self._mark_unavailable(
                canonical_id, parent_conid,
                reason=(
                    f"blocklisted conid (parent={parent_conid}, "
                    f"child={child_conid}) — known wrong-domain bind"
                ),
            )

        if self._dry_run:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="dry_run", child_conid=child_conid,
                detail=(
                    f"would have attached child conid {child_conid} "
                    f"(strike={yes_child.get('strike')} desc={yes_child.get('desc','')[:60]})"
                ),
            )

        # Tradeability check: snapshot the child twice (IBKR warms its
        # snapshot cache on the first call) before mutating the DB. A
        # non-tradeable child (e.g. an MLB-tagged conid that actually
        # points to a CPI/GDP economic indicator with no current market
        # data) would otherwise replace one bad conid with another and
        # the resolver would loop forever on the same parent.
        tradeable = False
        last_snap: Dict[str, Any] = {}
        try:
            for attempt_no in (1, 2, 3):
                await asyncio.sleep(1.0)
                snap = await self._client.market_snapshot(child_conid)
                last_snap = snap or {}
                # Import here to avoid a circular import at module load.
                from arbiter.collectors.forecastex import ForecastExClient
                if ForecastExClient.is_tradeable_snapshot(snap):
                    tradeable = True
                    break
        except Exception as exc:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="exception", child_conid=child_conid,
                detail=f"tradeability snapshot failed: {exc}",
            )

        if not tradeable:
            # IBKR returned a child conid but it has no live bid/ask —
            # this is the multi-strike CPI/GDP case where the parent
            # discovery matched the wrong product class. Mark the
            # mapping as forecastex_not_available so the resolver stops
            # retrying. Operator can still attach a known-good conid
            # via the manual override endpoint.
            return await self._mark_unavailable(
                canonical_id, parent_conid,
                reason=(
                    f"child {child_conid} resolved but not tradeable after 3 "
                    f"snapshot warmups (strike={yes_child.get('strike')} "
                    f"desc={(yes_child.get('desc') or '')[:60]}); "
                    f"raw snapshot keys={sorted(last_snap.keys())[:8]}"
                ),
            )

        # Persist the YES + NO child conids via the mapping store. The
        # mapping_store.upsert path triggers upsert_runtime_market_mapping
        # which mutates the in-process MARKET_MAP — so the next collector
        # cycle picks up both conids.
        # ForecastEx binary contracts have SEPARATE IBKR conids for YES
        # (right=C) and NO (right=P) under the same parent event at the
        # same strike. Persisting only YES was the root cause of the
        # ARB-000695/699 phantom-trade incident (2026-05-28 postmortem).
        no_child_conid = ""
        yes_strike_raw = yes_child.get("strike")
        try:
            yes_strike_f = (
                float(yes_strike_raw) if yes_strike_raw is not None else None
            )
        except (TypeError, ValueError):
            yes_strike_f = None
        for c in children:
            if not isinstance(c, dict):
                continue
            if str(c.get("right", "")).upper() not in ("P", "PUT"):
                continue
            if yes_strike_f is not None:
                try:
                    cs = float(c.get("strike") or 0.0)
                except (TypeError, ValueError):
                    continue
                if abs(cs - yes_strike_f) > 1e-6:
                    continue
            cand = str(c.get("conid") or "").strip()
            if cand and cand != child_conid:
                no_child_conid = cand
                break
        try:
            mapping = await self._mapping_store.get(canonical_id)
            if mapping is None:
                return ResolveAttempt(
                    canonical_id=canonical_id, parent_conid=parent_conid,
                    ts=now, outcome="exception", child_conid=child_conid,
                    detail="mapping disappeared between candidate selection and update",
                )
            mapping.forecastex_contract_id = child_conid
            if no_child_conid:
                mapping.forecastex_no_contract_id = no_child_conid
            mapping.forecastex_not_available = False
            await self._mapping_store.upsert(mapping)
        except Exception as exc:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="exception", child_conid=child_conid,
                detail=f"mapping_store.upsert failed: {exc}",
            )

        # Tell the collector the OLD parent is no longer reserved so any
        # future remap is unobstructed; the NEW child will be fresh too.
        try:
            self._collector.reactivate_conid(parent_conid)
            self._collector.reactivate_conid(child_conid)
            if no_child_conid:
                self._collector.reactivate_conid(no_child_conid)
        except Exception:
            pass

        logger.info(
            "forecastex_resolver.attached",
            canonical_id=canonical_id,
            parent_conid=parent_conid,
            child_conid=child_conid,
            no_child_conid=no_child_conid or "(unresolved)",
            right=yes_child.get("right"),
            strike=yes_child.get("strike"),
            source=yes_child.get("source"),
        )
        return ResolveAttempt(
            canonical_id=canonical_id, parent_conid=parent_conid,
            ts=now, outcome="resolved", child_conid=child_conid,
            detail=(
                f"attached child {child_conid} "
                f"(strike={yes_child.get('strike')} desc={(yes_child.get('desc') or '')[:60]})"
            ),
        )

    @staticmethod
    def _select_yes_child(
        canonical_id: str, children: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Choose the right Call child for this mapping.

        - Filter to Call (right=C) — IBKR's YES contract.
        - Group by strike value and pick the strike that matches the
          canonical_id's party/side hint when available (DEM/GOP for
          political control markets; Dem ↔ strike 1, GOP ↔ strike 2 per
          the IBKR HORC schema verified live 2026-05-25).
        - When no hint matches, pick the lowest strike (the canonical
          binary YES contract for 1-strike events).
        """
        calls = [c for c in children if str(c.get("right", "")).upper() == "C"]
        if not calls:
            return None
        # Sort by strike for deterministic selection.
        try:
            calls.sort(key=lambda c: float(c.get("strike") or 0))
        except (TypeError, ValueError):
            pass
        upper_cid = canonical_id.upper()
        # Political-control party hint: DEM/GOP map to strike 1 / 2 on
        # HORC and SENM per IBKR's schema.
        if "DEM_" in upper_cid or "DEMOCRAT" in upper_cid:
            return next(
                (c for c in calls if float(c.get("strike") or 0) <= 1.0001),
                calls[0],
            )
        if (
            "GOP_" in upper_cid or "REP_" in upper_cid
            or "REPUBLICAN" in upper_cid
        ):
            return next(
                (c for c in calls if float(c.get("strike") or 0) >= 1.9999),
                calls[-1],
            )
        return calls[0]

    async def _mark_unavailable(
        self, canonical_id: str, parent_conid: str, reason: str,
    ) -> ResolveAttempt:
        """Persist forecastex_not_available=true so the resolver stops
        re-trying the same parent every cycle. The mapping's
        forecastex_contract_id is preserved so an operator can see what
        was attempted; the not_available flag is what the
        forecastex_discovery skip and the collector candidate filter
        both honor.
        """
        now = time.time()
        if self._dry_run:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="dry_run",
                detail=f"would have marked not_available: {reason}",
            )
        try:
            # MarketMappingStore.upsert deliberately does NOT write the
            # forecastex_not_available column (it would race with the
            # single-column negative-cache writes), so the flag MUST go
            # through mark_forecastex_unavailable — an upsert here bumps
            # updated_at but silently drops the flag, and the mapping
            # re-enters the candidate pool on the next refresh_runtime_cache.
            marked = await self._mapping_store.mark_forecastex_unavailable(
                canonical_id, unavailable=True,
            )
            if not marked:
                return ResolveAttempt(
                    canonical_id=canonical_id, parent_conid=parent_conid,
                    ts=now, outcome="exception",
                    detail="mapping disappeared during not_available mark",
                )
            # Mirror into the in-process MARKET_MAP so the candidate filter
            # honors the quarantine before the next DB→memory refresh.
            from arbiter.config.settings import upsert_runtime_market_mapping

            upsert_runtime_market_mapping(
                canonical_id, {"forecastex_not_available": True},
            )
        except Exception as exc:
            return ResolveAttempt(
                canonical_id=canonical_id, parent_conid=parent_conid,
                ts=now, outcome="exception",
                detail=f"mark_unavailable write failed: {exc}",
            )
        # Drop the conid from the collector's inactive set so it stops
        # being a resolver candidate next cycle (the candidate filter
        # requires the conid to be in _inactive_conids).
        try:
            self._collector.reactivate_conid(parent_conid)
        except Exception:
            pass
        logger.info(
            "forecastex_resolver.marked_unavailable",
            canonical_id=canonical_id, parent_conid=parent_conid,
            reason=reason,
        )
        return ResolveAttempt(
            canonical_id=canonical_id, parent_conid=parent_conid,
            ts=now, outcome="marked_unavailable",
            detail=reason[:200],
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
