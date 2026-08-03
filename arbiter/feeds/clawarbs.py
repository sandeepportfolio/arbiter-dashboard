"""ClawArbs live-arbitrage feed consumer.

Polls https://clawarbs.com/api/predfeed.php and consumes ONLY the two-leg
``opportunities`` array (``type == "ARB_DETECTED"``). The ``value_bets``
array is one-leg directional exposure and is NEVER read — that is an
operator directive, not a preference.

Architecture: the feed is a SIGNAL SOURCE, never execution truth.

  1. A feed arb names venue market ids per leg. We resolve the Kalshi
     ticker against our mapping store. (The feed's "polymarket" is
     polymarket.com — a different venue from the Polymarket-US exchange we
     trade — so feed prices are advisory even when the event matches.)
  2. A resolved, CONFIRMED, auto-trade mapping with fresh two-sided quotes
     in OUR price store becomes an ArbitrageOpportunity priced from OUR
     books with OUR fee model. It is injected through
     ``monitor.alert_opportunity`` — the exact same gate chain (defensive
     re-validation, cooldown, executor preflight: mapping confirmation,
     edge floor, depth, staleness) every scanner opportunity faces. The
     feed cannot bypass anything.
  3. An UNRESOLVED Kalshi ticker is written to the mapping store as a
     discovery candidate so the dual-LLM review conveyor can confirm the
     pairing — the feed doubles as a mapping source.

Env:
  CLAWARBS_FEED_ENABLED     — master switch (default off)
  CLAWARBS_FEED_URL         — default https://clawarbs.com/api/predfeed.php
  CLAWARBS_POLL_SECONDS     — default 15 (site itself polls at 5s)
  CLAWARBS_MIN_EDGE_CENTS   — optional custom edge floor for feed-sourced
                              opportunities; the executor's own preflight
                              floor still applies on top (floors only ever
                              tighten, never loosen).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arbiter.feeds.clawarbs")

SUPPORTED_VENUES = {"kalshi", "polymarket"}
DEFAULT_FEED_URL = "https://clawarbs.com/api/predfeed.php"


@dataclass
class FeedArb:
    opportunity_id: str
    match_name: str
    market_type: str
    kalshi_ticker: str
    feed_edge_net_pct: float
    legs: List[Dict[str, Any]] = field(default_factory=list)


def parse_feed(payload: Any) -> List[FeedArb]:
    """Extract two-leg arbs from a predfeed payload.

    Reads ``opportunities`` ONLY. ``value_bets`` is intentionally never
    accessed; a feed arb must be type ARB_DETECTED with exactly two legs,
    at least one on Kalshi (our mapping anchor).
    """
    out: List[FeedArb] = []
    if not isinstance(payload, dict):
        return out
    for opp in payload.get("opportunities") or []:
        if not isinstance(opp, dict):
            continue
        if str(opp.get("type", "")) != "ARB_DETECTED":
            continue
        legs = opp.get("legs") or []
        if len(legs) != 2:
            continue
        venues = {str(leg.get("venue", "")).lower() for leg in legs}
        kalshi_legs = [
            leg for leg in legs if str(leg.get("venue", "")).lower() == "kalshi"
        ]
        if not kalshi_legs:
            # No Kalshi anchor -> we cannot resolve it to our catalog.
            continue
        out.append(FeedArb(
            opportunity_id=str(opp.get("opportunity_id", "")),
            match_name=str(opp.get("match_name", "")),
            market_type=str(opp.get("market_type", "")),
            kalshi_ticker=str(kalshi_legs[0].get("external_id", "")),
            feed_edge_net_pct=float(opp.get("edge_net_pct") or 0.0),
            legs=list(legs),
        ))
    return out


class ClawArbsFeed:
    """Poll loop + resolution + gated injection. See module docstring."""

    def __init__(
        self,
        *,
        monitor,
        price_store,
        mapping_store,
        scanner=None,
        feed_url: Optional[str] = None,
        poll_seconds: Optional[float] = None,
    ) -> None:
        self.monitor = monitor
        self.price_store = price_store
        self.mapping_store = mapping_store
        self.scanner = scanner
        self.feed_url = feed_url or os.getenv(
            "CLAWARBS_FEED_URL", DEFAULT_FEED_URL
        )
        try:
            self.poll_seconds = float(
                poll_seconds
                if poll_seconds is not None
                else os.getenv("CLAWARBS_POLL_SECONDS", "15")
            )
        except (TypeError, ValueError):
            self.poll_seconds = 15.0
        self.poll_seconds = max(5.0, self.poll_seconds)
        self._running = False
        self._seen: Dict[str, float] = {}  # opportunity_id -> last-injected ts
        self._seen_ttl = 600.0
        self._candidate_written: Dict[str, float] = {}
        self.stats: Dict[str, Any] = {
            "enabled": True,
            "last_poll_at": None,
            "last_poll_ok": None,
            "polls": 0,
            "feed_arbs_seen": 0,
            "tradable_pairs": 0,
            "injected": 0,
            "below_floor": 0,
            "unmapped": 0,
            "candidates_written": 0,
            "errors": 0,
            "last_error": "",
            "last_injected": [],
        }

    # ── feed IO ──────────────────────────────────────────────────────────
    async def fetch_feed(self) -> Optional[dict]:
        import aiohttp

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10.0)
            ) as session:
                async with session.get(
                    self.feed_url,
                    headers={"User-Agent": "arbiter-clawarbs-feed/1.0"},
                ) as resp:
                    if resp.status != 200:
                        self.stats["errors"] += 1
                        self.stats["last_error"] = f"HTTP {resp.status}"
                        return None
                    return await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)[:200]
            return None

    # ── resolution + injection ───────────────────────────────────────────
    async def _mapping_for_ticker(self, ticker: str):
        """Find our mapping whose kalshi_market_id matches the feed ticker."""
        getter = getattr(self.mapping_store, "get_by_platform", None)
        if callable(getter):
            try:
                return await getter("kalshi", ticker)
            except Exception as exc:  # noqa: BLE001
                logger.debug("clawarbs mapping lookup failed: %s", exc)
        return None

    def _min_edge_cents(self) -> float:
        try:
            custom = os.getenv("CLAWARBS_MIN_EDGE_CENTS", "")
            if custom.strip():
                return float(custom)
        except (TypeError, ValueError):
            pass
        scanner_cfg = getattr(self.scanner, "config", None)
        return float(getattr(scanner_cfg, "min_edge_cents", 7.0) or 7.0)

    async def _opportunity_from_our_books(self, mapping, feed_arb: FeedArb):
        """Price the arb from OUR venues' fresh quotes; None when not viable.

        Delegates opportunity construction to the scanner when it exposes
        ``build_opportunity_for_canonical`` (single source of fee math);
        otherwise returns None — we never hand-roll a second fee model.
        """
        builder = getattr(self.scanner, "build_opportunity_for_canonical", None)
        if not callable(builder):
            return None
        try:
            return await builder(mapping.canonical_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "clawarbs opportunity build failed for %s: %s",
                mapping.canonical_id, exc,
            )
            return None

    async def _write_candidate(self, feed_arb: FeedArb) -> None:
        """Surface an unmapped feed arb as a discovery candidate."""
        now = time.time()
        last = self._candidate_written.get(feed_arb.kalshi_ticker, 0.0)
        if now - last < 6 * 3600:
            return
        adder = getattr(self.mapping_store, "add_candidate", None)
        if not callable(adder):
            return
        poly_leg = next(
            (
                leg for leg in feed_arb.legs
                if str(leg.get("venue", "")).lower() != "kalshi"
            ),
            {},
        )
        try:
            await adder(
                canonical_id=f"CLAW_{feed_arb.kalshi_ticker}"[:190],
                platform="kalshi",
                platform_market_id=feed_arb.kalshi_ticker,
                description=(
                    f"{feed_arb.match_name} — clawarbs feed arb "
                    f"({feed_arb.feed_edge_net_pct:.1f}% net on feed venues; "
                    f"other leg {poly_leg.get('venue_label', '?')} "
                    f"{poly_leg.get('url', '')})"
                )[:500],
                match_score=0.5,
            )
            self._candidate_written[feed_arb.kalshi_ticker] = now
            self.stats["candidates_written"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("clawarbs candidate write failed: %s", exc)

    async def poll_once(self) -> Dict[str, Any]:
        self.stats["polls"] += 1
        self.stats["last_poll_at"] = time.time()
        payload = await self.fetch_feed()
        if payload is None:
            self.stats["last_poll_ok"] = False
            return self.stats
        self.stats["last_poll_ok"] = True

        feed_arbs = parse_feed(payload)
        self.stats["feed_arbs_seen"] += len(feed_arbs)
        now = time.time()
        # Expire dedup entries.
        self._seen = {
            k: v for k, v in self._seen.items() if now - v < self._seen_ttl
        }

        floor = self._min_edge_cents()
        for feed_arb in feed_arbs:
            if not feed_arb.kalshi_ticker:
                continue
            if feed_arb.opportunity_id in self._seen:
                continue
            mapping = await self._mapping_for_ticker(feed_arb.kalshi_ticker)
            status = str(getattr(mapping, "status", "") or "")
            status = status.split(".")[-1].lower()
            if mapping is None or status != "confirmed" or not getattr(
                mapping, "allow_auto_trade", False
            ):
                self.stats["unmapped"] += 1
                await self._write_candidate(feed_arb)
                continue
            self.stats["tradable_pairs"] += 1
            opp = await self._opportunity_from_our_books(mapping, feed_arb)
            if opp is None:
                continue
            net_edge_cents = float(getattr(opp, "net_edge_cents", 0.0) or 0.0)
            if net_edge_cents < floor:
                self.stats["below_floor"] += 1
                continue
            # Full gate chain: alert_opportunity re-validates safety and
            # queues for the auto-executor's preflight. Nothing bypassed.
            await self.monitor.alert_opportunity(opp)
            self._seen[feed_arb.opportunity_id] = now
            self.stats["injected"] += 1
            entry = {
                "opportunity_id": feed_arb.opportunity_id[:120],
                "canonical_id": getattr(mapping, "canonical_id", ""),
                "match": feed_arb.match_name[:80],
                "our_net_edge_cents": round(net_edge_cents, 2),
                "feed_edge_net_pct": feed_arb.feed_edge_net_pct,
                "at": now,
            }
            self.stats["last_injected"] = (
                [entry] + list(self.stats["last_injected"])
            )[:10]
            logger.info(
                "clawarbs: injected %s (%s) our_edge=%.2fc feed_edge=%.1f%%",
                getattr(mapping, "canonical_id", "?"),
                feed_arb.match_name[:60],
                net_edge_cents,
                feed_arb.feed_edge_net_pct,
            )
        return self.stats

    async def run(self) -> None:
        self._running = True
        logger.info(
            "clawarbs feed started url=%s poll=%.0fs floor=%.1fc",
            self.feed_url, self.poll_seconds, self._min_edge_cents(),
        )
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — loop must survive
                self.stats["errors"] += 1
                self.stats["last_error"] = str(exc)[:200]
                logger.warning("clawarbs poll error: %s", exc)
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        self._running = False


def clawarbs_feed_enabled() -> bool:
    return str(os.getenv("CLAWARBS_FEED_ENABLED", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )
