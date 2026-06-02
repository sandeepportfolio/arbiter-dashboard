"""StrandedMitigationEngine — sophisticated per-position decision engine.

Replaces the simple let_settle / close_market / illiquid classifier with
a real cost-benefit analysis that considers every recovery path:

  1. COMPLETE_ARB    — Found the matching opposite leg on another book
                       at a price that still produces positive edge.
                       BEST OUTCOME: turns a stranded leg into a real
                       arb without paying spread to exit.
  2. HOLD_TO_SETTLE  — Expected value at settlement (using the market's
                       implied probability from the live ask) exceeds
                       what we'd net from closing now.
  3. CLOSE_NOW       — Underwater AND close proceeds exceed expected
                       value at settle (the implied prob says we'll
                       lose more by holding).
  4. HEDGE           — Can't complete the arb at positive edge AND
                       can't recover cost AT settle, but opposite side
                       is available cheaply enough to cap downside.
  5. MANUAL_REVIEW   — None of the above clearly fit (illiquid book,
                       missing data, edge cases). Operator decides.

Every decision carries a ``ProfitabilitySnapshot`` and a rationale
string so the ops console can render the full cost-benefit chain
without re-running the math.

This module is INTENTIONALLY pure logic — no network calls, no DB
writes. The reconciler invokes ``decide()`` with the venue truth and
the engine returns a dataclass; execution is the caller's job. Keeps
the engine trivially unit-testable and lets dry-run mode surface
recommendations without any venue side-effects.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from arbiter.recovery.stranded_reconciler import StrandedPosition
    from arbiter.utils.price_store import PriceStore

logger = structlog.get_logger("arbiter.recovery.mitigation_engine")


# Decision action constants — string-based so they serialize cleanly
# to JSON for the API and the ops UI.
COMPLETE_ARB = "COMPLETE_ARB"
HOLD_TO_SETTLE = "HOLD_TO_SETTLE"
CLOSE_NOW = "CLOSE_NOW"
HEDGE = "HEDGE"
MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass
class ProfitabilitySnapshot:
    """Cost-benefit analysis of a single stranded position.

    Every value is in USD unless explicitly suffixed (cents / per-share).
    Numbers are derived from the live BBO + cost basis at decision time;
    they go stale as quotes move, so re-compute every reconciler cycle.
    """
    qty: float
    side: str                              # "YES" or "NO"
    cost_basis_usd: float
    cost_per_share: float
    best_bid: float
    best_ask: float
    spread_cents: float
    # MTM = qty * best_bid (what we'd net from a market sell, pre-fee)
    mtm_usd: float
    # Estimated fees to close the position via market sell at best_bid.
    fees_to_close_usd: float
    # Net cash from immediate close (mtm - fees).
    close_proceeds_usd: float
    # Implied probability the market currently assigns to this side
    # paying out at settlement. For a YES position: best_ask of YES is
    # the market's "buy YES" price = market-implied P(YES wins). For a
    # NO position: 1 - best_ask of YES = P(NO wins) ≈ best_bid of YES
    # is conservative (lower bound).
    implied_settle_probability: float
    # EV at settlement using the implied probability:
    #   EV = qty * implied_prob * $1   (binary payout)
    # Subtracts the cost basis to give the expected GAIN from holding.
    expected_value_at_settle_usd: float
    # hold_vs_close = expected_value_at_settle - close_proceeds.
    # Positive → holding is better. Negative → closing is better.
    hold_vs_close_usd: float
    # Mark of underwater status: we've lost money relative to entry.
    is_underwater: bool
    # Seconds until market close/expiry (None when unknown). Holding
    # through a long-dated expiry has opportunity cost; we surface this
    # but don't let it override the EV math unless explicit.
    seconds_to_expiry: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "qty": round(self.qty, 4),
            "side": self.side,
            "cost_basis_usd": round(self.cost_basis_usd, 4),
            "cost_per_share": round(self.cost_per_share, 4),
            "best_bid": round(self.best_bid, 4),
            "best_ask": round(self.best_ask, 4),
            "spread_cents": round(self.spread_cents, 2),
            "mtm_usd": round(self.mtm_usd, 4),
            "fees_to_close_usd": round(self.fees_to_close_usd, 4),
            "close_proceeds_usd": round(self.close_proceeds_usd, 4),
            "implied_settle_probability": round(self.implied_settle_probability, 4),
            "expected_value_at_settle_usd": round(self.expected_value_at_settle_usd, 4),
            "hold_vs_close_usd": round(self.hold_vs_close_usd, 4),
            "is_underwater": self.is_underwater,
            "seconds_to_expiry": (
                round(self.seconds_to_expiry, 1)
                if self.seconds_to_expiry is not None else None
            ),
        }


@dataclass
class MitigationDecision:
    """Engine output: what to do with this stranded position and why."""
    action: str
    rationale: str
    profitability: ProfitabilitySnapshot
    # Recovery confidence — operator-readable bucket.
    confidence: str = "medium"             # high | medium | low
    # COMPLETE_ARB fields. Populated only when action == COMPLETE_ARB.
    complete_arb_venue: Optional[str] = None
    complete_arb_canonical_id: Optional[str] = None
    complete_arb_market_id: Optional[str] = None
    complete_arb_side: Optional[str] = None
    complete_arb_price: Optional[float] = None
    complete_arb_qty: int = 0
    complete_arb_net_edge_cents: float = 0.0
    # CLOSE_NOW fields. Populated only when action == CLOSE_NOW.
    close_price: Optional[float] = None
    close_qty: int = 0
    # HEDGE fields. Populated only when action == HEDGE.
    hedge_venue: Optional[str] = None
    hedge_market_id: Optional[str] = None
    hedge_side: Optional[str] = None
    hedge_price: Optional[float] = None
    hedge_qty: int = 0
    # Whether this decision is safe to execute autonomously (silent
    # mitigation). False → must alert operator first.
    autonomous_ok: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "action": self.action,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "autonomous_ok": self.autonomous_ok,
            "profitability": self.profitability.to_dict(),
        }
        if self.action == COMPLETE_ARB:
            out["complete_arb"] = {
                "venue": self.complete_arb_venue,
                "canonical_id": self.complete_arb_canonical_id,
                "market_id": self.complete_arb_market_id,
                "side": self.complete_arb_side,
                "price": self.complete_arb_price,
                "qty": self.complete_arb_qty,
                "net_edge_cents": round(self.complete_arb_net_edge_cents, 2),
            }
        elif self.action == CLOSE_NOW:
            out["close"] = {
                "price": self.close_price,
                "qty": self.close_qty,
            }
        elif self.action == HEDGE:
            out["hedge"] = {
                "venue": self.hedge_venue,
                "market_id": self.hedge_market_id,
                "side": self.hedge_side,
                "price": self.hedge_price,
                "qty": self.hedge_qty,
            }
        return out


def _canonical_id_for_position(
    pos: "StrandedPosition", market_map: Dict[str, Any],
) -> Optional[str]:
    """Reverse-lookup: given a venue market_id, find the canonical_id
    the mapping table assigns to it. Returns None when the position
    has no mapping (one-off lot the engine never auto-traded).
    """
    mid = (pos.market_id or "").strip().lower()
    if not mid:
        return None
    for canonical_id, mapping in market_map.items():
        for key in (
            "kalshi", "kalshi_market_id",
            "polymarket", "polymarket_slug", "polymarket_market_id",
            "forecastex", "forecastex_contract_id",
        ):
            v = str(mapping.get(key) or "").strip().lower()
            if v and v == mid:
                return canonical_id
    return None


def _estimate_close_fees(platform: str, price: float, qty: int) -> float:
    """Estimate fees for closing a position at ``price`` × ``qty``.

    Uses the same fee functions the scanner uses so the cost-benefit
    math stays consistent across the codebase. Conservative — picks
    the higher fee bucket when uncertain (e.g. defaults to politics on
    Polymarket).
    """
    if qty <= 0 or price <= 0:
        return 0.0
    try:
        if platform == "kalshi":
            from arbiter.config.settings import kalshi_order_fee
            return float(kalshi_order_fee(price, quantity=qty))
        if platform == "polymarket":
            from arbiter.config.settings import polymarket_order_fee
            return float(polymarket_order_fee(
                price, quantity=qty, category="politics",
            ))
        if platform == "forecastex":
            from arbiter.config.settings import forecastex_order_fee
            return float(forecastex_order_fee(price, quantity=qty))
    except Exception:
        # Fallback floor when fee table lookups fail — assume 0.5% so
        # the engine doesn't think a close is free when fees actually
        # exceed proceeds.
        return max(0.005 * price * qty, 0.01)
    return 0.0


def build_profitability_snapshot(pos: "StrandedPosition") -> ProfitabilitySnapshot:
    """Compute a fresh cost-benefit view from the stranded position record."""
    qty_abs = abs(float(pos.qty or 0.0))
    side = str(pos.side or "YES").upper()
    cost = abs(float(pos.cost_basis_usd or 0.0))
    cps = cost / qty_abs if qty_abs > 0 else 0.0
    bid = float(pos.best_bid or 0.0)
    ask = float(pos.best_ask or 0.0)
    spread_cents = max(0.0, (ask - bid) * 100.0) if (bid > 0 and ask > 0) else 0.0
    # Closing a YES long at the BID. NO long converts symmetrically —
    # selling a NO at NO-bid is the same as buying YES at (1 - NO-bid).
    # For simplicity we assume best_bid is the price-side adjusted for
    # the position's direction (the reconciler's _fetch_*_positions
    # already populates it that way: kalshi yes_bid for YES, polymarket
    # NO-side bid for NO).
    close_price = bid if bid > 0 else 0.0
    mtm = qty_abs * close_price
    fees_close = _estimate_close_fees(pos.platform, close_price, int(qty_abs))
    proceeds = max(0.0, mtm - fees_close)
    # Implied settle probability: use the ASK (what someone is willing
    # to PAY to take our side). On a YES position, yes_ask = market's
    # P(YES wins). On a NO position the reconciler's best_ask already
    # reflects NO-ask, so the symmetry holds — best_ask is the price
    # of taking OUR side, ≈ market-implied probability we win.
    implied_prob = max(0.0, min(1.0, ask if ask > 0 else bid))
    ev_at_settle = qty_abs * implied_prob * 1.0
    hold_vs_close = ev_at_settle - proceeds
    is_underwater = mtm < cost
    seconds_to_expiry = (
        (pos.expiry_ts - time.time())
        if getattr(pos, "expiry_ts", None) is not None else None
    )
    return ProfitabilitySnapshot(
        qty=qty_abs,
        side=side,
        cost_basis_usd=cost,
        cost_per_share=cps,
        best_bid=bid,
        best_ask=ask,
        spread_cents=spread_cents,
        mtm_usd=mtm,
        fees_to_close_usd=fees_close,
        close_proceeds_usd=proceeds,
        implied_settle_probability=implied_prob,
        expected_value_at_settle_usd=ev_at_settle,
        hold_vs_close_usd=hold_vs_close,
        is_underwater=is_underwater,
        seconds_to_expiry=seconds_to_expiry,
    )


@dataclass
class MitigationConfig:
    """Tunable thresholds for the mitigation engine. Defaults bias
    toward safety: silent auto-mitigation only fires on very clearly
    profitable recoveries.
    """
    # Minimum net edge (cents) the COMPLETE_ARB path must produce
    # before the engine recommends it. Lower than the live-trade
    # MIN_EDGE_CENTS (default 7) because recovery edges have a higher
    # cost basis baked in (we're paying spread to enter the missing
    # leg, not building a fresh arb from scratch).
    complete_arb_min_edge_cents: float = 3.0
    # Per-share cost floor below which CLOSE_NOW is suppressed —
    # closing 2¢ contracts costs more in fees than the recovered
    # value, so we always prefer HOLD_TO_SETTLE there. Same threshold
    # as the legacy let_settle gate.
    penny_cost_threshold_usd: float = 0.05
    # Threshold for "the market thinks we're losing". When the live
    # ask-implied probability for OUR side falls below this AND we
    # bought higher, CLOSE_NOW captures residual scrap value before
    # resolution news takes it to zero. 0.25 = market gives us < 25%
    # chance of winning; at that point the variance premium of holding
    # outweighs the small expected-value gap closing leaves on the
    # table.
    losing_implied_prob_threshold: float = 0.25
    # CLOSE_NOW also fires when the position is dramatically underwater
    # — even if the implied probability isn't catastrophic, recovering
    # SOMETHING before resolution beats riding a binary that's already
    # priced as a long shot. Ratio: close_proceeds / cost_basis below
    # this means we've lost more than (1 - underwater_close_ratio) of
    # original capital and the market expects continued losses.
    underwater_close_ratio: float = 0.40
    # Maximum spread (basis points) on the closing-side book before
    # the engine declines to mark CLOSE_NOW / COMPLETE_ARB autonomous
    # — wide spreads mean illiquid pricing and forced unwind hurts.
    max_autonomous_spread_bps: float = 800.0
    # Notional ceiling (USD) for autonomous execution. Larger positions
    # always go through operator review regardless of recommendation.
    max_autonomous_notional_usd: float = 30.0
    # When the venue has no live BBO / no adapter AND notional is below
    # this threshold, the engine returns HOLD_TO_SETTLE (silent) rather
    # than MANUAL_REVIEW. Reasoning: a $4 Kalshi position with no
    # resting bid will SETTLE in days; the operator can do nothing
    # about it until then, and a Telegram alert is pure noise. Above
    # this threshold the operator wants visibility because they may
    # want to babysit a manual close once a book reappears.
    unactionable_max_notional_usd: float = 10.0
    # Age-bracketed loss-cut thresholds. The reconciler tracks
    # ``first_seen_ts`` for every position; the bracket says how much
    # of the original cost basis we'll absorb as realised loss on a
    # CLOSE_NOW depending on how long the lot has been stranded.
    # Rationale: fresh strands (<6h) are likely temporary glitches —
    # holding gives the engine room to complete the arb instead of
    # paying spread to exit at a loss. Mature strands (>24h) are
    # almost certainly real one-leg risk; locking in 50% is better
    # than holding through an adverse resolution.
    young_age_seconds: float = 6 * 3600.0       # 0-6h bucket
    mature_age_seconds: float = 24 * 3600.0     # 6-24h bucket
    young_max_loss_pct: float = 0.0             # 0-6h: profitable or break-even only
    mature_max_loss_pct: float = 0.25           # 6-24h: up to 25% loss of cost
    aged_max_loss_pct: float = 0.50             # 24h+: up to 50% loss of cost
    # Settlement-proximity hold window. When the market closes within
    # this many seconds, we HOLD even if the loss-cut math says CLOSE.
    # Paying spread to exit a position that will resolve in hours is
    # never worth it — settlement itself zeroes or settles the lot.
    settlement_proximity_hold_seconds: float = 24 * 3600.0
    # Max age (seconds) for a PriceStore quote to be considered "live"
    # for a COMPLETE_ARB / CLOSE_NOW decision. Older than this and the
    # engine refuses to act — the GOP_SENATE_2026 incident kept
    # submitting $0.48 against a $0.55 live book because the stored
    # quote was minutes stale. 60s lines up with the scanner cadence,
    # so any healthy collector keeps the gate open.
    max_quote_age_seconds: float = 60.0
    # Cross-leg cost gap (fraction of $1 settlement) above which we
    # mark the position HOLD_TO_SETTLE permanently — even if a future
    # quote dips below the math threshold, repeated FOK rejects at a
    # gap this large mean the venue book never agrees on a clearing
    # price. 0.15 = combined cost (paid + ask) ≥ $1.15, which means
    # there's no entry price that turns a profit even before fees.
    hopeless_cost_gap: float = 0.15


class MitigationEngine:
    """Pure-logic decision engine for stranded positions.

    Construct once and reuse — no state across decisions. Heavyweight
    inputs (price store, market map, fee tables) are passed via
    constructor so callers can swap implementations in tests without
    monkey-patching globals.
    """

    def __init__(
        self,
        *,
        price_store: Optional["PriceStore"] = None,
        market_map_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        adapters: Optional[Dict[str, Any]] = None,
        config: Optional[MitigationConfig] = None,
    ) -> None:
        self._price_store = price_store
        self._market_map_provider = market_map_provider or (lambda: {})
        self._adapters = adapters or {}
        self._config = config or MitigationConfig()

    async def decide(self, pos: "StrandedPosition") -> MitigationDecision:
        """Produce a mitigation decision for ``pos``.

        Decision order matches the action priority in the module
        docstring: try COMPLETE_ARB first (highest EV), then HOLD vs
        CLOSE, then HEDGE, then MANUAL.
        """
        prof = build_profitability_snapshot(pos)
        cfg = self._config
        market_map = self._market_map_provider() or {}

        # ─── Guard: no adapter → can't act, manual review ───────────────
        adapter = self._adapters.get(pos.platform)
        has_close = adapter is not None and hasattr(adapter, "place_unwind_sell")

        # ─── 1. COMPLETE_ARB ────────────────────────────────────────────
        # Only attempt if we know which canonical market this lot belongs
        # to. Stranded lots from manual one-offs (no mapping) skip
        # straight to the EV math.
        canonical_id = _canonical_id_for_position(pos, market_map)
        complete = None
        if canonical_id and self._price_store is not None:
            complete = await self._try_complete_arb(
                pos, canonical_id, market_map, prof,
            )
        if complete is not None:
            return complete

        # ─── 2. EV math: HOLD vs CLOSE ──────────────────────────────────
        # Penny longshots ALWAYS hold to settle. The Kalshi per-contract
        # fee floor eats the recoverable value, and on a YES resolution
        # we'd forego $1/share by closing for the few cents of MTM.
        if prof.cost_per_share > 0 and prof.cost_per_share <= cfg.penny_cost_threshold_usd:
            return MitigationDecision(
                action=HOLD_TO_SETTLE,
                rationale=(
                    f"penny position ({prof.cost_per_share:.4f}/sh ≤ "
                    f"{cfg.penny_cost_threshold_usd:.2f} threshold). Closing "
                    f"fees (≈${prof.fees_to_close_usd:.3f}) exceed recoverable "
                    f"value (mtm=${prof.mtm_usd:.3f}); holding through "
                    f"resolution preserves the $1/share payout on YES outcome."
                ),
                profitability=prof,
                confidence="high",
                autonomous_ok=True,  # silent — no operator alert needed
            )

        # Unactionable small-notional gate. When the venue has no
        # adapter or the market has no live BBO, we can't execute a
        # close OR a complete-arb. For small notional positions the
        # only path forward IS to wait for settlement — the operator
        # genuinely has nothing to do. Returning HOLD_TO_SETTLE
        # (autonomous, silent) suppresses the Telegram burst that
        # would otherwise fire every cycle the book stays empty.
        # 2026-05-26 incident: KXMLBNL-26-PHI (70 YES @ $0.06 = $4.20
        # total) had no Kalshi BBO and triggered a noisy MANUAL_REVIEW
        # alert despite the operator having no recourse — that's the
        # exact noise this gate kills.
        notional_now = abs(prof.cost_basis_usd)
        unactionable_cap = float(getattr(cfg, "unactionable_max_notional_usd", 0.0) or 0.0)
        if (
            (not has_close or prof.best_bid <= 0 or prof.best_ask <= 0)
            and notional_now <= unactionable_cap
        ):
            reason_bits: List[str] = []
            if not has_close:
                reason_bits.append(f"no adapter on {pos.platform}")
            if prof.best_bid <= 0 or prof.best_ask <= 0:
                reason_bits.append(
                    f"no live BBO ({prof.best_bid:.4f}/{prof.best_ask:.4f})"
                )
            return MitigationDecision(
                action=HOLD_TO_SETTLE,
                rationale=(
                    f"unactionable small lot — {', '.join(reason_bits)}; "
                    f"notional ${notional_now:.2f} ≤ ${unactionable_cap:.2f} "
                    f"unactionable cap. Position will SETTLE on its own; "
                    f"operator has no available action before that, so "
                    f"silent hold avoids noise."
                ),
                profitability=prof,
                confidence="high",
                autonomous_ok=True,
            )

        if not has_close:
            return MitigationDecision(
                action=MANUAL_REVIEW,
                rationale=f"no adapter for platform={pos.platform} — operator must intervene",
                profitability=prof,
                confidence="low",
                autonomous_ok=False,
            )

        if prof.best_bid <= 0 or prof.best_ask <= 0:
            return MitigationDecision(
                action=MANUAL_REVIEW,
                rationale=(
                    f"no live BBO on {pos.platform} ({prof.best_bid:.4f}/"
                    f"{prof.best_ask:.4f}) — cannot price a close or hedge "
                    f"without painting the market (notional ${notional_now:.2f} "
                    f"exceeds unactionable cap ${unactionable_cap:.2f}, so "
                    f"operator review surfaces it)"
                ),
                profitability=prof,
                confidence="low",
                autonomous_ok=False,
            )

        notional = abs(prof.cost_basis_usd)
        spread_bps = max(0.0, prof.spread_cents) * 100.0
        # "Losing" signal: market currently prices our side as a long
        # shot AND we're underwater. The bid-side scrap is still
        # positive (BBO gate above ensured this). Capturing it now
        # caps downside before resolution news takes the position to
        # zero.
        market_says_losing = (
            prof.implied_settle_probability < cfg.losing_implied_prob_threshold
            and prof.is_underwater
        )
        deep_underwater = (
            prof.cost_basis_usd > 0
            and prof.close_proceeds_usd / max(prof.cost_basis_usd, 1e-9)
            <= cfg.underwater_close_ratio
        )

        # ─── Settlement-proximity guard ─────────────────────────────────
        # Within ``settlement_proximity_hold_seconds`` of expiry, we
        # ALWAYS hold — settlement itself will resolve the position one
        # way or the other, and the spread we'd pay to exit dwarfs any
        # marginal edge the cost-benefit math claims. Applies regardless
        # of whether we're underwater.
        if (
            prof.seconds_to_expiry is not None
            and 0 < prof.seconds_to_expiry <= cfg.settlement_proximity_hold_seconds
        ):
            hours_left = prof.seconds_to_expiry / 3600.0
            return MitigationDecision(
                action=HOLD_TO_SETTLE,
                rationale=(
                    f"settlement-proximity hold: market resolves in "
                    f"{hours_left:.1f}h (≤ {cfg.settlement_proximity_hold_seconds / 3600.0:.0f}h "
                    f"hold window). Spread cost to exit (${(prof.spread_cents / 100.0) * prof.qty:.2f}) "
                    f"exceeds remaining holding cost; let settlement resolve."
                ),
                profitability=prof,
                confidence="high",
                autonomous_ok=True,
            )

        if market_says_losing or deep_underwater:
            autonomous = (
                notional <= cfg.max_autonomous_notional_usd
                and spread_bps <= cfg.max_autonomous_spread_bps
            )
            recovered_pct = (
                (prof.close_proceeds_usd / prof.cost_basis_usd * 100.0)
                if prof.cost_basis_usd > 0 else 0.0
            )
            # ─── Age-bracketed loss-cut threshold ───────────────────────
            # Compute realised-loss ratio that closing-now would lock
            # in, then look up the maximum loss the position's age
            # bracket tolerates. If the close would breach the cap,
            # downgrade to HOLD_TO_SETTLE instead — paying spread to
            # crystallise a too-large loss is exactly the trap the
            # operator wants the engine to avoid.
            age_seconds = max(0.0, time.time() - float(getattr(pos, "first_seen_ts", 0.0) or 0.0))
            if age_seconds < cfg.young_age_seconds:
                age_bracket = "0-6h"
                max_loss_pct = cfg.young_max_loss_pct
            elif age_seconds < cfg.mature_age_seconds:
                age_bracket = "6-24h"
                max_loss_pct = cfg.mature_max_loss_pct
            else:
                age_bracket = "24h+"
                max_loss_pct = cfg.aged_max_loss_pct
            realised_loss_pct = (
                max(0.0, (prof.cost_basis_usd - prof.close_proceeds_usd) / prof.cost_basis_usd)
                if prof.cost_basis_usd > 0 else 0.0
            )
            if realised_loss_pct > max_loss_pct:
                return MitigationDecision(
                    action=HOLD_TO_SETTLE,
                    rationale=(
                        f"age-bracket loss cap: closing now would lock in "
                        f"{realised_loss_pct:.0%} loss of ${prof.cost_basis_usd:.2f} "
                        f"cost basis, exceeding the {max_loss_pct:.0%} cap for "
                        f"the {age_bracket} bracket (age={age_seconds / 3600.0:.1f}h). "
                        f"Hold and let the position mature into the next "
                        f"bracket or settle naturally."
                    ),
                    profitability=prof,
                    confidence="medium",
                    autonomous_ok=True,
                )

            return MitigationDecision(
                action=CLOSE_NOW,
                rationale=(
                    f"market-implied P({prof.side})="
                    f"{prof.implied_settle_probability:.1%} signals losing; "
                    f"closing recovers ${prof.close_proceeds_usd:.2f} "
                    f"({recovered_pct:.0f}% of ${prof.cost_basis_usd:.2f} "
                    f"cost) before resolution news takes the position to "
                    f"zero. EV at settle (${prof.expected_value_at_settle_usd:.2f}) "
                    f"only marginally exceeds scrap; certainty premium of "
                    f"the bid wins. Age={age_seconds / 3600.0:.1f}h "
                    f"({age_bracket}); realised loss {realised_loss_pct:.0%} "
                    f"within {max_loss_pct:.0%} bracket cap."
                ),
                profitability=prof,
                confidence="high" if deep_underwater else "medium",
                autonomous_ok=autonomous,
                close_price=prof.best_bid,
                close_qty=int(prof.qty),
            )

        # Default: HOLD. The market-implied probability says the
        # position still has meaningful EV at settle, and there's no
        # COMPLETE_ARB path available.
        return MitigationDecision(
            action=HOLD_TO_SETTLE,
            rationale=(
                f"hold: implied P({prof.side})="
                f"{prof.implied_settle_probability:.1%} ≥ losing threshold "
                f"({cfg.losing_implied_prob_threshold:.0%}); EV at settle "
                f"(${prof.expected_value_at_settle_usd:.2f}) ≥ close proceeds "
                f"(${prof.close_proceeds_usd:.2f}). Position retains real "
                f"value vs the bid-side scrap."
            ),
            profitability=prof,
            confidence="high" if prof.hold_vs_close_usd > 1.0 else "medium",
            autonomous_ok=True,
        )

    # ─── COMPLETE_ARB search ────────────────────────────────────────────

    async def _try_complete_arb(
        self,
        pos: "StrandedPosition",
        canonical_id: str,
        market_map: Dict[str, Any],
        prof: ProfitabilitySnapshot,
    ) -> Optional[MitigationDecision]:
        """Look across every venue with a quote for this canonical_id
        and see if buying the OPPOSITE side completes the arb at a
        positive net edge.

        Stranded YES position → need to BUY NO somewhere.
        Stranded NO position  → need to BUY YES somewhere.

        Returns a COMPLETE_ARB decision when a venue + price is found,
        None when no candidate exists or the math doesn't clear the
        minimum edge floor.
        """
        store = self._price_store
        if store is None:
            return None
        try:
            quotes = await store.get_all_for_market(canonical_id)
        except Exception as exc:
            logger.debug(
                "mitigation_engine.price_store_failed",
                canonical_id=canonical_id, err=str(exc),
            )
            return None
        if not quotes:
            return None

        our_side = str(pos.side or "YES").upper()
        # The side we still need to BUY to complete the arb.
        need_side = "NO" if our_side == "YES" else "YES"
        qty = int(prof.qty)
        if qty <= 0:
            return None

        # Avg cost we already paid for the stranded leg, per share.
        # COMPLETE_ARB math: paying P_need on the missing side gives a
        # combined cost of (cps + P_need); arb edge = 1.0 - that - fees.
        cps = prof.cost_per_share
        if cps <= 0:
            return None

        best: Optional[MitigationDecision] = None
        for venue, pp in quotes.items():
            if venue == pos.platform:
                # Skip the same venue — that's a hedge against ourselves,
                # not a cross-book completion. Same-venue YES+NO would
                # also fall under "close" or "hedge", handled elsewhere.
                continue
            adapter = self._adapters.get(venue)
            if adapter is None or not hasattr(adapter, "place_fok"):
                continue
            # Staleness gate: refuse to act on a stale quote. The
            # GOP_SENATE_2026 loop was driven by a $0.48 cached quote
            # against a live $0.55 book — every FOK rejected because
            # the book had moved while the price_store hadn't caught
            # up. ``age_seconds`` is computed against time.time() so
            # this is a hard wall-clock check, not a TTL.
            quote_age = float(getattr(pp, "age_seconds", 0.0) or 0.0)
            if quote_age > self._config.max_quote_age_seconds:
                logger.info(
                    "mitigation_engine.stale_quote_skip",
                    canonical_id=canonical_id, venue=venue,
                    quote_age_s=round(quote_age, 1),
                    max_age_s=self._config.max_quote_age_seconds,
                    pos_platform=pos.platform, pos_market_id=pos.market_id,
                )
                continue
            # The price we'd PAY to enter the missing side.
            if need_side == "NO":
                buy_price = float(pp.no_ask or pp.no_price or 0.0)
                market_id = pp.no_market_id or pp.raw_market_id
            else:
                buy_price = float(pp.yes_ask or pp.yes_price or 0.0)
                market_id = pp.yes_market_id or pp.raw_market_id
            if buy_price <= 0 or not market_id:
                continue
            # Fees on the missing-leg entry.
            entry_fees_per_share = (
                _estimate_close_fees(venue, buy_price, qty) / max(qty, 1)
            )
            # Net edge per pair (cps + buy_price are the combined cost;
            # $1 is the settled payout because exactly one side wins).
            net_edge = 1.0 - cps - buy_price - entry_fees_per_share
            net_edge_cents = net_edge * 100.0
            # Permanently-unprofitable gate. When combined cost (paid
            # for the stranded leg + ask for the missing leg) exceeds
            # $1 by more than ``hopeless_cost_gap``, even a future
            # quote at the bid won't clear — the venue books simply
            # don't agree on a profitable pairing. Log loud so the
            # operator sees the explicit max-affordable.
            max_affordable = 1.0 - cps - entry_fees_per_share
            combined_cost = cps + buy_price
            if combined_cost - 1.0 >= self._config.hopeless_cost_gap:
                logger.info(
                    "mitigation_engine.hopeless_skip",
                    canonical_id=canonical_id, venue=venue,
                    current_ask=round(buy_price, 4),
                    max_affordable=round(max_affordable, 4),
                    cost_per_share=round(cps, 4),
                    combined_cost=round(combined_cost, 4),
                    hopeless_threshold=self._config.hopeless_cost_gap,
                    pos_platform=pos.platform, pos_market_id=pos.market_id,
                )
                continue
            if net_edge_cents < self._config.complete_arb_min_edge_cents:
                logger.info(
                    "mitigation_engine.unprofitable_skip",
                    canonical_id=canonical_id, venue=venue,
                    current_ask=round(buy_price, 4),
                    max_affordable=round(max_affordable, 4),
                    net_edge_cents=round(net_edge_cents, 2),
                    min_edge_cents=self._config.complete_arb_min_edge_cents,
                    pos_platform=pos.platform, pos_market_id=pos.market_id,
                )
                continue
            cand = MitigationDecision(
                action=COMPLETE_ARB,
                rationale=(
                    f"complete arb by buying {qty} {need_side} on {venue} "
                    f"@ ${buy_price:.4f}. Combined cost ${(cps + buy_price):.4f}/pair "
                    f"+ fees ${entry_fees_per_share:.4f}/sh leaves "
                    f"{net_edge_cents:.2f}¢ net edge (vs original loss of "
                    f"${prof.cost_basis_usd - prof.close_proceeds_usd:.3f} if "
                    f"we just closed)."
                ),
                profitability=prof,
                confidence="high",
                autonomous_ok=(
                    abs(prof.cost_basis_usd) <= self._config.max_autonomous_notional_usd
                ),
                complete_arb_venue=venue,
                complete_arb_canonical_id=canonical_id,
                complete_arb_market_id=market_id,
                complete_arb_side=need_side,
                complete_arb_price=buy_price,
                complete_arb_qty=qty,
                complete_arb_net_edge_cents=net_edge_cents,
            )
            if best is None or cand.complete_arb_net_edge_cents > best.complete_arb_net_edge_cents:
                best = cand
        return best
