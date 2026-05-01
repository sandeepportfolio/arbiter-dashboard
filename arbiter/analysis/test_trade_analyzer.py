"""Unit tests for the deterministic trade analyzer.

The analyzer takes a structured snapshot of an arb and returns markdown.
Every code path needs at least one assertion that the resulting markdown
mentions the distinguishing feature (e.g. "401" in the auth-failure case)
so that drift in section wording fails loudly rather than silently dropping
diagnostic detail.
"""
from datetime import datetime, timezone

from .trade_analyzer import (
    TradeAnalyzerInput,
    analyze_trade,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _make_opp(**overrides):
    base = {
        "canonical_id": "GAME_TEST_42",
        "description": "Test market",
        "yes_platform": "kalshi",
        "yes_price": 0.30,
        "yes_fee": 0.02,
        "no_platform": "polymarket",
        "no_price": 0.65,
        "no_fee": 0.015,
        "gross_edge": 0.05,
        "total_fees": 0.035,
        "net_edge": 0.015,
        "net_edge_cents": 1.5,
        "suggested_qty": 10,
        "max_profit_usd": 0.15,
        "yes_bid": 0.29,
        "yes_ask": 0.30,
        "no_bid": 0.64,
        "no_ask": 0.65,
        "quote_age_seconds": 4.2,
        "min_available_liquidity": 60.0,
        "persistence_count": 3,
    }
    base.update(overrides)
    return base


def _make_order(side, status, *, fill_qty=0.0, fill_price=0.0, error="", platform=None):
    return {
        "order_id": f"ARB-000001-{side.upper()}-{platform or ('KALSHI' if side == 'yes' else 'POLY')}",
        "platform": platform or ("kalshi" if side == "yes" else "polymarket"),
        "side": side,
        "price": 0.30 if side == "yes" else 0.65,
        "quantity": 10.0,
        "status": status,
        "fill_qty": fill_qty,
        "fill_price": fill_price or (0.30 if side == "yes" else 0.65),
        "error": error,
        "submitted_at": datetime(2026, 4, 30, 9, 1, 0, tzinfo=timezone.utc),
        "terminal_at": None,
    }


# ─── Closed (winning) arb ────────────────────────────────────────────────────


def test_closed_arb_renders_verdict_and_realized_pnl():
    md = analyze_trade(
        TradeAnalyzerInput(
            arb_id="ARB-000203",
            canonical_id="GAME_NHL_VGK",
            status="closed",
            realized_pnl=12.36,
            opportunity=_make_opp(suggested_qty=30, net_edge_cents=43.61, max_profit_usd=13.08),
            orders=[
                _make_order("yes", "filled", fill_qty=30.0, fill_price=0.30),
                _make_order("no", "filled", fill_qty=30.0, fill_price=0.65),
            ],
        )
    )
    assert "ARB-000203" in md
    assert "✅" in md
    assert "Both legs executed" in md
    assert "$+12.36" in md
    assert "## Edge Math" in md
    assert "43.61" in md  # net edge cents preserved
    assert "Clean execution" in md  # default suggestion path


# ─── Auth-failure arb ────────────────────────────────────────────────────────


def test_kalshi_401_is_called_out_explicitly():
    err = (
        'Kalshi API 401: {"error":{"code":"authentication_error",'
        '"message":"authentication_error"}}'
    )
    md = analyze_trade(
        TradeAnalyzerInput(
            arb_id="ARB-000007",
            canonical_id="LOSEMAJORITY",
            status="failed",
            realized_pnl=0.0,
            opportunity=_make_opp(),
            orders=[
                _make_order("yes", "failed", error=err),
                _make_order(
                    "no",
                    "aborted",
                    error="Skipped: primary leg did not fill (sequential execution)",
                ),
            ],
        )
    )
    assert "## Verdict" in md
    assert "401" in md
    # Sanity: both diagnostic and recommendation surfaces mention auth.
    assert "authentication" in md.lower()
    # Sequential-skip pattern must produce its specific recommendation.
    assert "sequential" in md.lower() or "primary leg did not fill" in md.lower()


# ─── Naked-leg / recovering arb ──────────────────────────────────────────────


def test_recovering_arb_flags_naked_exposure():
    md = analyze_trade(
        TradeAnalyzerInput(
            arb_id="ARB-000012",
            canonical_id="LOSEMAJORITY",
            status="recovering",
            realized_pnl=0.0,
            opportunity=_make_opp(),
            orders=[
                _make_order("yes", "filled", fill_qty=10.0, fill_price=0.14, platform="kalshi"),
                _make_order("no", "cancelled", platform="polymarket"),
            ],
        )
    )
    assert "Naked-leg exposure" in md
    assert "YES" in md  # filled side called out
    assert "Unhedged exposure" in md
    # Recommendation should mention the audit-trail gap explicitly.
    assert "unwind" in md.lower() or "directional risk" in md.lower()


# ─── Stuck-pending arb (no orders submitted) ─────────────────────────────────


def test_pending_arb_with_no_orders_diagnoses_gate_block():
    md = analyze_trade(
        TradeAnalyzerInput(
            arb_id="ARB-000013",
            canonical_id="GAME_NHL_EDM",
            status="pending",
            realized_pnl=0.0,
            opportunity=_make_opp(),
            orders=[],
        )
    )
    assert "No orders were ever submitted" in md
    # Suggestion path should explicitly call out the trade-gate hypothesis.
    assert "trade gate" in md.lower() or "gate" in md.lower()


# ─── Simulation ──────────────────────────────────────────────────────────────


def test_simulation_marks_dry_run_in_header():
    md = analyze_trade(
        TradeAnalyzerInput(
            arb_id="ARB-000999",
            canonical_id="GAME_TEST",
            status="simulated",
            realized_pnl=0.0,
            is_simulation=True,
            opportunity=_make_opp(),
            orders=[],
        )
    )
    assert "*(simulation)*" in md
    assert "Dry-run simulation" in md


# ─── Edge-math math sanity ───────────────────────────────────────────────────


def test_edge_math_table_prints_sum_and_net():
    md = analyze_trade(
        TradeAnalyzerInput(
            arb_id="ARB-X",
            canonical_id="C",
            status="filled",
            realized_pnl=0.0,
            opportunity=_make_opp(yes_price=0.30, no_price=0.65, gross_edge=0.05),
            orders=[
                _make_order("yes", "filled", fill_qty=10.0, fill_price=0.30),
                _make_order("no", "filled", fill_qty=10.0, fill_price=0.65),
            ],
        )
    )
    # Sum is 0.95; gross edge 0.05; both must appear.
    assert "0.9500" in md
    assert "0.0500" in md
    # Top-of-book line shows the bid/ask snapshot.
    assert "bid=0.2900" in md
