"""Phase 5 live-fire helpers — B-2 (fee fetchers) + B-3 (opportunity builder).

ALL functions are REAL (non-stub) implementations. No ``raise NotImpl...`` stubs,
no placeholder pass-throughs. Each helper has an associated unit test in
``test_live_fire_helpers.py`` that asserts it actually calls the underlying
adapter / price store (the anti-pattern being defended against is a bare
``raise`` stub that silently passes the reconcile step and masks real fee drift).
T-5-02-09 in the threat register: a bare ``raise`` stub in these helpers would
cause ``fee_fetcher`` to return 0.0, which ``reconcile_post_trade`` compares
against the fee-model computation and — if the drift is under the tolerance by
coincidence — silently passes reconcile without catching a real breach.

Deliverables:
  * ``build_opportunity_from_quotes`` (B-3): builds an ArbitrageOpportunity from
    current PriceStore quotes using the same cross-platform pattern the scanner
    uses in ``ArbitrageScanner._build_cross_platform_opportunity``. Returns None
    when no tradable arb exists (edge too thin, notional > cap, missing prices).
  * ``fetch_kalshi_platform_fee`` (B-2): authenticated GET on Kalshi's
    /portfolio/fills endpoint; sums fee_cents across matching fills.
  * ``fetch_polymarket_platform_fee`` (B-2): invokes the CLOB client's
    ``get_trades(market=<condition_id>)`` via ``asyncio.to_thread`` (the CLOB
    client is sync) and sums fee_usd on matching trades.
  * ``write_pre_trade_requote`` (W-3): writes side-by-side original + requoted
    opportunity snapshots to ``evidence_dir/pre_trade_requote.json``.

Module constants:
  * ``PRE_EXECUTION_OPERATOR_ABORT_SECONDS = 60.0`` (W-6 — CLAUDE.md 'Safety > speed';
    the operator watches this window to ARM the kill-switch if anything looks wrong
    about the opportunity being placed).
  * ``POLYGON_SETTLEMENT_WAIT_SECONDS = 60.0`` (RESEARCH Q5; tunable after first run).
  * ``TEST_PER_LEG_USD_CEILING = 10.0`` (belt above PHASE5_MAX_ORDER_USD).

Notes on adapter shape:
  The production KalshiAdapter exposes ``self.session`` / ``self.auth`` /
  ``self.config.kalshi.base_url`` (public attributes, no leading underscore).
  The production PolymarketAdapter exposes a ``clob_client_factory`` callable
  bound to ``self._get_client`` — not a cached ``_clob_client`` attribute — so
  the Polymarket helper pulls the client through that factory. Per-order
  condition-id resolution is cached under ``adapter._order_condition_index``
  (a dict ``{order_id: condition_id}``) that the live-fire test populates after
  each place_fok call; see ``test_first_live_trade.py`` for the wiring pattern.
  If a future refactor renames either adapter attribute, unit tests will catch
  the drift BEFORE the live-fire ever runs (AsyncMock.assert_awaited-with).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

log = structlog.get_logger("arbiter.live.live_fire_helpers")

__all__ = [
    "PRE_EXECUTION_OPERATOR_ABORT_SECONDS",
    "POLYGON_SETTLEMENT_WAIT_SECONDS",
    "TEST_PER_LEG_USD_CEILING",
    "build_opportunity_from_quotes",
    "fetch_kalshi_platform_fee",
    "fetch_polymarket_platform_fee",
    "write_pre_trade_requote",
]

# ─── Module constants ─────────────────────────────────────────────────────────

#: W-6: CLAUDE.md 'Safety > speed' — 60 seconds between opportunity build and
#: engine.execute(). Operator scrutinizes the opportunity and ARMs the kill if
#: anything looks off (wrong canonical_id, stale price, wrong platform).
PRE_EXECUTION_OPERATOR_ABORT_SECONDS: float = 60.0

#: RESEARCH Q5 decision — 60 seconds for Polygon block confirmation + CLOB
#: indexer catch-up before reconcile. Tunable after the first live-fire run.
POLYGON_SETTLEMENT_WAIT_SECONDS: float = 60.0

#: Belt above PHASE5_MAX_ORDER_USD. The adapter hard-lock is the primary cap
#: ($10); this constant is a test-side redundancy check: even if an operator
#: accidentally sets PHASE5_MAX_ORDER_USD higher, the helper will still refuse
#: opportunities whose suggested_qty * price exceeds $10 per leg.
TEST_PER_LEG_USD_CEILING: float = 10.0


# ─── B-3: opportunity builder ─────────────────────────────────────────────────


async def build_opportunity_from_quotes(
    price_store,
    canonical_id: str,
    per_leg_cap_usd: float = TEST_PER_LEG_USD_CEILING,
):
    """Build an ArbitrageOpportunity from current PriceStore quotes for one canonical_id.

    Uses ``ArbitrageScanner._build_cross_platform_opportunity`` as the pattern
    analog so the fee math, yes/no side assignment, and suggested_qty computation
    match the scanner. The helper is async because ``PriceStore.get_all_for_market``
    is async — the test body awaits this helper directly (no ``asyncio.run``).

    Returns None when:
      * quotes are missing for both platforms, or only one platform has a quote;
      * the best (yes, no) pairing yields no tradable arb (gross_edge <= 0 or
        net_edge_cents below the scanner's min_edge_cents floor);
      * suggested_qty * price exceeds ``per_leg_cap_usd`` on either leg (the
        scanner's own position sizer may recommend a qty > $10-per-leg if the
        ScannerConfig.max_position_usd is larger; this helper enforces a tighter
        belt so a misconfigured ScannerConfig cannot accidentally size over the
        PHASE5 hard-lock).

    Args:
        price_store: an ``arbiter.utils.price_store.PriceStore`` instance.
        canonical_id: the canonical market id (e.g. 'CAN-SPX-2024').
        per_leg_cap_usd: maximum USD notional per leg. Default $10 (PHASE5 belt).

    Returns:
        Best-edge ``ArbitrageOpportunity`` within the cap, or None.
    """
    # Lazy imports so the helpers module stays cheap to import (and unit tests
    # in test_live_fire_helpers.py don't pay the scanner's import cost).
    from types import SimpleNamespace

    from arbiter.scanner.arbitrage import ArbitrageScanner

    prices = await price_store.get_all_for_market(canonical_id)
    if not prices or len(prices) < 2:
        log.info(
            "live_fire.build_opportunity.insufficient_quotes",
            canonical_id=canonical_id,
            platforms=list(prices.keys()) if prices else [],
        )
        return None

    # Construct a lightweight scanner instance without running __init__ side
    # effects (the scanner's __init__ does not touch the network but allocates
    # queues/deques we don't need here). Only the attributes
    # _build_cross_platform_opportunity reads are required:
    scanner = ArbitrageScanner.__new__(ArbitrageScanner)
    # ``_build_cross_platform_opportunity`` + ``_compute_confidence`` read the
    # following fields off ``self.config``; construct the minimal stub.
    scanner.config = SimpleNamespace(
        # The scanner floor for publishing a tradable opportunity. 1.0¢ is the
        # same floor Plan 04 used as the sandbox default. Gates net_edge_cents.
        min_edge_cents=1.0,
        # Position sizer: capital_limited = max_position_usd / (yes+no).
        # Passing per_leg_cap_usd*2 here ensures capital_limited doesn't bite
        # before the explicit per-leg cap check below.
        max_position_usd=max(per_leg_cap_usd * 2.0, 1.0),
        # Confidence inputs — matter for freshness_score + liquidity_score but
        # not for the tradability decision (the helper does not gate on
        # confidence). Use permissive defaults.
        max_quote_age_seconds=60.0,
        min_liquidity=1.0,
    )

    platforms = list(prices.keys())
    best = None
    for yes_platform in platforms:
        for no_platform in platforms:
            if yes_platform == no_platform:
                continue
            yes_point = prices[yes_platform]
            no_point = prices[no_platform]
            try:
                opp = scanner._build_cross_platform_opportunity(
                    canonical_id,
                    canonical_id,  # description = canonical_id here; no mapping dict
                    "confirmed",
                    1.0,
                    yes_point,
                    no_point,
                )
            except Exception as exc:
                log.warning(
                    "live_fire.build_opportunity.scanner_error",
                    err=str(exc),
                    yes_platform=yes_platform,
                    no_platform=no_platform,
                )
                continue
            if opp is None:
                continue
            # Per-leg cap (PHASE5 belt — belt-and-suspenders above
            # the adapter hard-lock).
            if opp.suggested_qty * opp.yes_price > per_leg_cap_usd + 1e-9:
                log.info(
                    "live_fire.build_opportunity.over_cap_yes_leg",
                    canonical_id=canonical_id,
                    suggested_qty=opp.suggested_qty,
                    yes_price=opp.yes_price,
                    cap=per_leg_cap_usd,
                )
                continue
            if opp.suggested_qty * opp.no_price > per_leg_cap_usd + 1e-9:
                log.info(
                    "live_fire.build_opportunity.over_cap_no_leg",
                    canonical_id=canonical_id,
                    suggested_qty=opp.suggested_qty,
                    no_price=opp.no_price,
                    cap=per_leg_cap_usd,
                )
                continue
            if best is None or opp.net_edge_cents > best.net_edge_cents:
                best = opp

    if best is None:
        log.info(
            "live_fire.build_opportunity.no_tradable_arb",
            canonical_id=canonical_id,
        )
    else:
        log.info(
            "live_fire.build_opportunity.selected",
            canonical_id=canonical_id,
            yes_platform=best.yes_platform,
            no_platform=best.no_platform,
            net_edge_cents=best.net_edge_cents,
            suggested_qty=best.suggested_qty,
        )
    return best


# ─── B-2: Kalshi platform fee fetcher ─────────────────────────────────────────


async def fetch_kalshi_platform_fee(adapter, order_id: str) -> float:
    """Query Kalshi's fills endpoint for the given order_id; sum fee_cents.

    GET ``{base_url}/portfolio/fills?order_id=<id>`` via the adapter's
    authenticated session. Kalshi's endpoint may return fills for other orders
    in the same response window, so we defensively filter by ``fill.order_id``
    before summing.

    Args:
        adapter: a ``KalshiAdapter`` with ``session`` / ``auth`` / ``config.kalshi.base_url``.
        order_id: the Kalshi order_id returned by ``adapter.place_fok``.

    Returns:
        Platform-reported total fee in USD (sum of fee_cents / 100.0).

    Anti-stub guarantee (T-5-02-09): this function MUST call ``session.get``.
    ``test_fetch_kalshi_platform_fee_happy_path_sums_fee_cents`` asserts it.
    """
    session = getattr(adapter, "session", None) or getattr(adapter, "_session", None)
    auth = getattr(adapter, "auth", None) or getattr(adapter, "_auth", None)
    # Base URL can live at adapter.config.kalshi.base_url OR (legacy/mocks)
    # adapter._base_url. Check both.
    base_url = None
    config = getattr(adapter, "config", None)
    if config is not None:
        kalshi_cfg = getattr(config, "kalshi", None)
        if kalshi_cfg is not None:
            base_url = getattr(kalshi_cfg, "base_url", None)
    if not base_url:
        base_url = getattr(adapter, "_base_url", None)

    assert session is not None, (
        "fetch_kalshi_platform_fee: adapter missing .session (or ._session) — "
        "KalshiAdapter API drift?"
    )
    assert auth is not None, (
        "fetch_kalshi_platform_fee: adapter missing .auth (or ._auth) — "
        "KalshiAdapter API drift?"
    )
    assert base_url, (
        "fetch_kalshi_platform_fee: base_url unresolved — checked "
        "adapter.config.kalshi.base_url and adapter._base_url"
    )

    # Kalshi base_url may or may not end with /trade-api/v2. We pass the path
    # component to auth.get_headers (it signs the path) and build the URL by
    # concatenation. If base_url already contains /trade-api/v2 (typical), we
    # pass the leaf path /portfolio/fills for signing.
    path = "/trade-api/v2/portfolio/fills"
    headers = auth.get_headers("GET", path)
    url = f"{base_url.rstrip('/')}/portfolio/fills"
    params = {"order_id": order_id}

    async with session.get(url, params=params, headers=headers) as resp:
        resp.raise_for_status()
        body = await resp.json()

    fills = body.get("fills") or []
    total_cents = 0.0
    for fill in fills:
        if fill.get("order_id") != order_id:
            continue
        try:
            total_cents += float(fill.get("fee_cents", 0) or 0)
        except (TypeError, ValueError):
            # Malformed fee_cents: treat as 0 but surface via log; reconcile
            # will see the gap if Kalshi really returned a non-numeric value.
            log.warning(
                "live_fire.fetch_kalshi_fee.bad_fee_cents",
                order_id=order_id,
                raw=fill.get("fee_cents"),
            )
    total_usd = total_cents / 100.0
    log.info(
        "live_fire.fetch_kalshi_fee",
        order_id=order_id,
        total_usd=total_usd,
        fill_count=len(fills),
    )
    return total_usd


# ─── B-2: Polymarket platform fee fetcher ─────────────────────────────────────


async def fetch_polymarket_platform_fee(adapter, order_id: str) -> float:
    """Sum platform-reported fees for the given order_id via Polymarket's CLOB client.

    Polymarket's ``client.get_trades`` returns market-scoped trade records (A6).
    The condition_id (market id) is NOT embedded in the order_id, so the live-fire
    test must cache ``{order_id: condition_id}`` on the adapter as
    ``_order_condition_index`` after each ``place_fok`` call. We refuse to proceed
    on a cache miss (AssertionError) rather than silently returning 0.0 and masking
    a reconcile breach — T-5-02-09 anti-stub defense.

    Args:
        adapter: a ``PolymarketAdapter`` with ``_get_client()`` (the
            ``clob_client_factory`` bound by ``__init__``) and an
            ``_order_condition_index`` dict populated by the test harness.
        order_id: the Polymarket order_id returned by ``adapter.place_fok``.

    Returns:
        Platform-reported total fee in USD.
    """
    # The real PolymarketAdapter binds its factory to ``self._get_client``; some
    # test mocks use a ``client`` attribute directly. Handle both.
    clob = None
    getter = getattr(adapter, "_get_client", None)
    if callable(getter):
        clob = getter()
    if clob is None:
        clob = getattr(adapter, "_clob_client", None) or getattr(adapter, "client", None)
    assert clob is not None, (
        "fetch_polymarket_platform_fee: could not obtain CLOB client from "
        "adapter._get_client() / adapter._clob_client / adapter.client"
    )

    order_index = getattr(adapter, "_order_condition_index", None) or {}
    condition_id = order_index.get(order_id)
    assert condition_id, (
        f"fetch_polymarket_platform_fee: no cached condition_id for "
        f"order_id={order_id!r}; adapter._order_condition_index is empty or "
        f"missing this key. The live-fire test must populate this cache after "
        f"adapter.place_fok returns so reconcile can find the trade."
    )

    # CLOB client is synchronous (py-clob-client). Wrap in asyncio.to_thread to
    # avoid blocking the event loop during the live-fire reconcile window.
    trades = await asyncio.to_thread(clob.get_trades, market=condition_id)

    total = 0.0
    match_count = 0
    for trade in trades or []:
        if isinstance(trade, dict):
            trade_order_id = trade.get("order_id")
            fee = trade.get("fee_usd", 0.0)
        else:
            trade_order_id = getattr(trade, "order_id", None)
            fee = getattr(trade, "fee_usd", 0.0)
        if trade_order_id != order_id:
            continue
        try:
            total += float(fee or 0.0)
            match_count += 1
        except (TypeError, ValueError):
            log.warning(
                "live_fire.fetch_polymarket_fee.bad_fee_usd",
                order_id=order_id,
                raw=fee,
            )

    log.info(
        "live_fire.fetch_polymarket_fee",
        order_id=order_id,
        condition_id=condition_id,
        total_usd=total,
        matched_trades=match_count,
    )
    return total


# ─── W-3: pre-trade requote evidence writer ───────────────────────────────────


def write_pre_trade_requote(evidence_dir, requoted_opp, original_opp=None) -> Path:
    """Write pre_trade_requote.json with original + requoted opportunity snapshots.

    Written BEFORE the 60-second operator-abort sleep so the on-disk artifact
    exists even if the operator ARMs the kill-switch during the pause (W-3 fix:
    earlier draft wrote this AFTER engine.execute, which meant the evidence
    was lost on abort).

    Args:
        evidence_dir: path-like directory under evidence/05/.
        requoted_opp: the ``ArbitrageOpportunity`` after a fresh re-quote.
        original_opp: optional — the ``ArbitrageOpportunity`` as first detected;
            if provided, both are stored side-by-side so the operator can see
            any drift between initial detection and final placement.

    Returns:
        pathlib.Path of the written JSON file.
    """
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / "pre_trade_requote.json"
    payload: Dict[str, Any] = {}
    if original_opp is not None:
        payload["original"] = (
            original_opp.to_dict()
            if hasattr(original_opp, "to_dict") and callable(original_opp.to_dict)
            else repr(original_opp)
        )
    payload["requoted"] = (
        requoted_opp.to_dict()
        if hasattr(requoted_opp, "to_dict") and callable(requoted_opp.to_dict)
        else repr(requoted_opp)
    )
    out.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    log.info("live_fire.write_pre_trade_requote", path=str(out))
    return out
