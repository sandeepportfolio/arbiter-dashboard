"""Generate a human-readable markdown post-mortem for any arbitrage attempt.

The analyzer is **deterministic and offline** — it reads structured execution
state (arb row, leg orders, fills, incidents, opportunity JSON) and produces
a multi-section markdown report. No external service / LLM call. That keeps
generation cheap (sub-millisecond), reproducible across runs, and safe to
invoke from inside the execution path.

Usage:

    md = analyze_trade(TradeAnalyzerInput(arb_row=..., orders=[...], ...))
    # or, given an asyncpg connection and an arb_id:
    md = await analyze_arb_from_db(conn, "ARB-000203")

Bump ``ANALYZER_VERSION`` whenever the output format changes; the engine
records the version alongside the markdown so a future backfill can detect
stale analyses and refresh them.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Bumping invalidates cached analyses (older rows can be force-refreshed via
# the backfill script when the format changes meaningfully).
ANALYZER_VERSION = 1


# ─── Inputs ──────────────────────────────────────────────────────────────────


@dataclass
class TradeAnalyzerInput:
    """All the data the analyzer needs. Build from DB rows or from in-memory ArbExecution."""

    arb_id: str
    canonical_id: str
    status: str
    realized_pnl: float
    net_edge: Optional[float] = None
    is_simulation: bool = False
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    opportunity: Mapping[str, Any] = field(default_factory=dict)
    orders: Sequence[Mapping[str, Any]] = field(default_factory=list)
    fills: Sequence[Mapping[str, Any]] = field(default_factory=list)
    incidents: Sequence[Mapping[str, Any]] = field(default_factory=list)


# ─── Public API ──────────────────────────────────────────────────────────────


def analyze_trade(data: TradeAnalyzerInput) -> str:
    sections = [
        _section_header(data),
        _section_verdict(data),
        _section_edge_breakdown(data),
        _section_leg_timeline(data),
        _section_venue_responses(data),
        _section_outcome(data),
        _section_what_could_be_different(data),
    ]
    return "\n\n".join(s for s in sections if s).strip() + "\n"


async def analyze_arb_from_db(conn, arb_id: str) -> str:
    """Pull every related row and run the analyzer in one call.

    Raises ``LookupError`` if the arb does not exist.
    """
    arb_row = await conn.fetchrow(
        "SELECT arb_id, canonical_id, status, net_edge, realized_pnl, "
        "       opportunity_json, is_simulation, created_at, closed_at "
        "FROM execution_arbs WHERE arb_id = $1",
        arb_id,
    )
    if arb_row is None:
        raise LookupError(f"arb not found: {arb_id}")

    order_rows = await conn.fetch(
        "SELECT order_id, platform, side, price, quantity, status, fill_price, "
        "       fill_qty, error, submitted_at, terminal_at "
        "FROM execution_orders WHERE arb_id = $1 ORDER BY submitted_at ASC",
        arb_id,
    )
    fill_rows = await conn.fetch(
        "SELECT f.fill_id, f.order_id, f.price, f.quantity, f.fees_paid, f.filled_at, "
        "       o.platform, o.side "
        "FROM execution_fills f JOIN execution_orders o USING (order_id) "
        "WHERE o.arb_id = $1 ORDER BY f.filled_at ASC",
        arb_id,
    )
    incident_rows = await conn.fetch(
        "SELECT incident_id, severity, message, status, created_at, resolved_at, "
        "       resolution_note "
        "FROM execution_incidents WHERE arb_id = $1 ORDER BY created_at ASC",
        arb_id,
    )

    opp = arb_row["opportunity_json"]
    if isinstance(opp, str):
        try:
            opp = json.loads(opp)
        except json.JSONDecodeError:
            opp = {}
    elif opp is None:
        opp = {}

    data = TradeAnalyzerInput(
        arb_id=arb_row["arb_id"],
        canonical_id=arb_row["canonical_id"],
        status=arb_row["status"],
        realized_pnl=float(arb_row["realized_pnl"] or 0),
        net_edge=float(arb_row["net_edge"]) if arb_row["net_edge"] is not None else None,
        is_simulation=bool(arb_row["is_simulation"]),
        created_at=arb_row["created_at"],
        closed_at=arb_row["closed_at"],
        opportunity=opp,
        orders=[dict(r) for r in order_rows],
        fills=[dict(r) for r in fill_rows],
        incidents=[dict(r) for r in incident_rows],
    )
    return analyze_trade(data)


# ─── Section builders ────────────────────────────────────────────────────────


def _section_header(d: TradeAnalyzerInput) -> str:
    desc = d.opportunity.get("description") or d.canonical_id
    sim_tag = " *(simulation)*" if d.is_simulation else ""
    when = _fmt_ts(d.created_at) or "-"
    return (
        f"# {d.arb_id} — {_status_emoji(d.status)} `{d.status}`{sim_tag}\n"
        f"**Market:** {desc}  \n"
        f"**Canonical:** `{d.canonical_id}`  \n"
        f"**Created:** {when}"
    )


def _section_verdict(d: TradeAnalyzerInput) -> str:
    s = (d.status or "").lower()
    pnl = float(d.realized_pnl or 0)
    qty = _opp_int(d.opportunity, "suggested_qty", 0)
    edge_c = _opp_float(d.opportunity, "net_edge_cents", 0.0)
    expected = (edge_c / 100.0) * qty
    yes_status, no_status = _leg_statuses(d)

    if s in {"filled", "closed", "settled"}:
        verdict = (
            f"Both legs executed and the arbitrage was captured. "
            f"Realized **${pnl:+.2f}** (expected ≈ ${expected:+.2f} on a {qty}-contract trade at "
            f"{edge_c:.2f}¢ net edge)."
        )
    elif s == "simulated":
        verdict = (
            "Dry-run simulation — no real orders submitted. "
            f"Notional expected P&L would have been ${expected:+.2f}."
        )
    elif s == "recovering":
        filled_side = "YES" if yes_status == "filled" and no_status != "filled" else "NO"
        verdict = (
            f"**Naked-leg exposure**: the {filled_side} leg filled but the other side did not. "
            "The recovery loop attempts to unwind the filled leg at market; realized P&L will "
            "reflect the unwind slippage rather than the original edge."
        )
    elif s in {"failed", "aborted"}:
        why = _diagnose_failure(d)
        verdict = f"Trade did **not** execute. {why}"
    elif s in {"submitted", "pending"}:
        verdict = (
            "Order is still in flight. Either both legs are working their way to fills, or the "
            "system was restarted before the fill loop finished. The recovery process picks these up."
        )
    else:
        verdict = "State is non-standard — see the leg timeline below."

    return f"## Verdict\n{verdict}"


def _section_edge_breakdown(d: TradeAnalyzerInput) -> str:
    o = d.opportunity or {}
    if not o:
        return "## Edge Math\n_No opportunity snapshot stored — arb was created without a quote payload._"

    yes_p = _opp_float(o, "yes_price")
    no_p = _opp_float(o, "no_price")
    yes_v = (o.get("yes_platform") or "").lower() or "yes-venue"
    no_v = (o.get("no_platform") or "").lower() or "no-venue"
    yes_fee = _opp_float(o, "yes_fee")
    no_fee = _opp_float(o, "no_fee")
    gross = _opp_float(o, "gross_edge", 1.0 - yes_p - no_p)
    total_fees = _opp_float(o, "total_fees", yes_fee + no_fee)
    net = _opp_float(o, "net_edge", gross - total_fees)
    net_c = _opp_float(o, "net_edge_cents", net * 100.0)
    qty = _opp_int(o, "suggested_qty")
    max_profit = _opp_float(o, "max_profit_usd", net * qty)

    bid_ask = []
    for tag, side in (("yes", "YES"), ("no", "NO")):
        bid = o.get(f"{tag}_bid")
        ask = o.get(f"{tag}_ask")
        if bid is not None and ask is not None:
            bid_ask.append(f"{side}: bid={float(bid):.4f} / ask={float(ask):.4f}")
    bid_ask_line = " · ".join(bid_ask) or "_no bid/ask snapshot_"

    quote_age = o.get("quote_age_seconds") or max(
        float(o.get("yes_quote_age_seconds") or 0),
        float(o.get("no_quote_age_seconds") or 0),
    )
    liq = o.get("min_available_liquidity")
    persist = o.get("persistence_count")

    table = (
        "| Component | Value |\n"
        "|---|---|\n"
        f"| Buy YES on **{yes_v}** | {yes_p:.4f} ({yes_p*100:.2f}¢) |\n"
        f"| Buy NO on **{no_v}** | {no_p:.4f} ({no_p*100:.2f}¢) |\n"
        f"| Sum of legs | **{yes_p + no_p:.4f}** |\n"
        f"| Gross edge (1 − sum) | **{gross:.4f}** ({gross*100:.2f}¢) |\n"
        f"| YES fee per contract | {yes_fee:.4f} |\n"
        f"| NO fee per contract | {no_fee:.4f} |\n"
        f"| Total fees | {total_fees:.4f} ({total_fees*100:.2f}¢) |\n"
        f"| **Net edge** | **{net:.4f} ({net_c:.2f}¢)** |\n"
        f"| Suggested qty | {qty} contracts |\n"
        f"| Expected max profit | ${max_profit:+.2f} |"
    )

    extras = [f"Top of book: {bid_ask_line}"]
    if quote_age:
        extras.append(f"Quote age at detection: {float(quote_age):.1f}s")
    if liq is not None:
        extras.append(f"Min available liquidity: ${float(liq):.0f}")
    if persist:
        extras.append(f"Persistence: {int(persist)} consecutive scan(s)")

    return "## Edge Math\n" + table + "\n\n" + "  \n".join(extras)


def _section_leg_timeline(d: TradeAnalyzerInput) -> str:
    if not d.orders:
        return (
            "## Leg-by-leg Execution\n"
            "_No orders were ever submitted to either venue._ "
            "This typically means the trade gate blocked execution after the arb was created "
            "(e.g. PnL reconciliation drift, balance threshold, or open critical incident), "
            "and the placeholder `pending` row was left in place."
        )

    fills_by_order: Dict[str, List[Mapping[str, Any]]] = {}
    for f in d.fills:
        fills_by_order.setdefault(str(f["order_id"]), []).append(f)

    rows: List[str] = []
    for o in d.orders:
        platform = (o.get("platform") or "?").lower()
        side = (o.get("side") or "?").upper()
        price = _to_float(o.get("price"))
        qty = _to_float(o.get("quantity"))
        fill_qty = _to_float(o.get("fill_qty"))
        fill_px = _to_float(o.get("fill_price"))
        status = (o.get("status") or "?").lower()
        ts = _fmt_ts(o.get("submitted_at"))
        emoji = _order_emoji(status, fill_qty, qty)
        order_id = str(o.get("order_id") or "")
        line = (
            f"- {emoji} **{platform}/{side}** `{order_id}` — "
            f"placed {qty:g} @ ${price:.4f} → "
            f"**{status}**, filled {fill_qty:g}/{qty:g}"
        )
        if fill_qty > 0:
            line += f" @ ${fill_px:.4f} (notional ${fill_qty * fill_px:.2f})"
        if ts:
            line += f"  \n  _submitted {ts}_"
        err = (o.get("error") or "").strip()
        if err:
            line += f"  \n  _error_: `{_truncate(err, 320)}`"
        these_fills = fills_by_order.get(order_id, [])
        if these_fills:
            line += "  \n  _fills_: " + "; ".join(
                f"{_to_float(fr['quantity']):g} @ ${_to_float(fr['price']):.4f}"
                f" (fee ${_to_float(fr.get('fees_paid')):.4f})"
                for fr in these_fills
            )
        rows.append(line)

    return "## Leg-by-leg Execution\n" + "\n".join(rows)


def _section_venue_responses(d: TradeAnalyzerInput) -> str:
    errors = [
        (str(o.get("order_id") or ""), o.get("platform") or "", o.get("side") or "", o.get("error") or "")
        for o in d.orders
        if (o.get("error") or "").strip()
    ]
    if not errors:
        return ""
    parts = ["## Venue Responses"]
    for oid, plat, side, err in errors:
        parts.append(f"**{plat}/{side}** `{oid}`:\n```\n{_truncate(err, 800)}\n```")
    return "\n\n".join(parts)


def _section_outcome(d: TradeAnalyzerInput) -> str:
    s = (d.status or "").lower()
    pnl = float(d.realized_pnl or 0)
    yes_filled, no_filled = _leg_filled_qty(d)

    lines: List[str] = []
    if s in {"filled", "closed", "settled"}:
        lines.append(f"- Realized P&L: **${pnl:+.2f}**")
        if d.closed_at:
            lines.append(f"- Closed at: {_fmt_ts(d.closed_at)}")
    elif s == "recovering":
        lines.append(
            f"- Filled side: **{'YES' if yes_filled > 0 else 'NO'}** "
            f"({max(yes_filled, no_filled):g} contracts)"
        )
        lines.append(f"- Realized P&L (so far): **${pnl:+.2f}**")
        lines.append(
            "- Unhedged exposure remains until the recovery loop completes the unwind."
        )
    elif s in {"failed", "aborted"}:
        lines.append(f"- Realized P&L: **${pnl:+.2f}** (no capture)")
        if yes_filled or no_filled:
            lines.append(
                f"- ⚠️ Partial fill of {max(yes_filled, no_filled):g} contracts — verify "
                "the platform position is flat."
            )
    elif s in {"submitted", "pending"}:
        lines.append("- No realized P&L yet — orders still working or never submitted.")

    crit = [i for i in d.incidents if (i.get("severity") or "").lower() == "critical"]
    open_crit = [i for i in crit if (i.get("status") or "").lower() == "open"]
    if crit:
        lines.append(
            f"- Incidents: {len(crit)} critical, {len(open_crit)} still open"
            + ("; investigate the open ones." if open_crit else " — all resolved.")
        )

    if not lines:
        return ""
    return "## Outcome\n" + "\n".join(lines)


def _section_what_could_be_different(d: TradeAnalyzerInput) -> str:
    s = (d.status or "").lower()
    suggestions: List[str] = []
    yes_filled, no_filled = _leg_filled_qty(d)
    err_blob = " ".join((o.get("error") or "").lower() for o in d.orders)

    if "401" in err_blob or "authentication_error" in err_blob:
        suggestions.append(
            "Kalshi returned an authentication error. The session-keepalive fix "
            "(commit `412f6df`) addresses the recurring 401s; if this resurfaces "
            "post-fix, rotate the API key and verify the private key path is mounted."
        )
    if "internal_server_error" in err_blob:
        suggestions.append(
            "Venue returned a 5xx. Retry policy should already cover transient "
            "outages — confirm the circuit breaker isn't swallowing legitimate retries."
        )
    if "primary leg did not fill" in err_blob:
        suggestions.append(
            "Sequential execution aborted the second leg because the first didn't fill. "
            "Consider concurrent placement for high-confidence opportunities, or widen "
            "the IOC tolerance on the first leg."
        )
    if s == "recovering" or (s in {"failed", "aborted"} and (yes_filled or no_filled)):
        suggestions.append(
            "One leg filled while the other did not — this leaves directional risk. "
            "Verify the recovery loop unwound on-platform, and persist the unwind orders "
            "to `execution_orders` so the audit trail captures the full lifecycle."
        )
    qty = _opp_int(d.opportunity, "suggested_qty")
    edge_c = _opp_float(d.opportunity, "net_edge_cents")
    if s in {"failed", "aborted"} and edge_c and edge_c < 5.0:
        suggestions.append(
            f"Net edge was only {edge_c:.2f}¢ — at this margin a single point of slippage "
            "or a tighter spread can flip the trade negative. Consider raising "
            "`min_edge_cents` for this market category, or reducing position size."
        )
    if s == "pending" and not d.orders:
        suggestions.append(
            "Arb was created but no legs ever submitted. Check the trade gate "
            "(`PnL reconciliation drift`, `balances`, `incidents`) at the time of detection — "
            "it likely blocked, and the pending stub was never reaped."
        )
    if s in {"filled", "closed"} and qty and qty < 5:
        suggestions.append(
            "Position size was small. With a clean fill on both legs, this market "
            "could justify a larger size next time — review per-market caps."
        )

    if not suggestions:
        if s in {"filled", "closed", "settled"}:
            suggestions.append("Clean execution. No corrective action required.")
        else:
            suggestions.append("No specific recommendation surfaced from the structured data.")

    return "## What Could Be Different Next Time\n" + "\n".join(f"- {s_}" for s_ in suggestions)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _diagnose_failure(d: TradeAnalyzerInput) -> str:
    err_blob = " ".join((o.get("error") or "").lower() for o in d.orders)
    if not d.orders:
        return (
            "No orders were submitted to either venue — the trade gate likely blocked "
            "execution at the moment of detection (typical causes: PnL reconciliation drift, "
            "low balance, open critical incident, or the market mapping wasn't auto-tradable)."
        )
    if "401" in err_blob or "authentication_error" in err_blob:
        return "Kalshi rejected the order with HTTP 401 (authentication_error)."
    if "internal_server_error" in err_blob:
        return "The venue returned a 5xx error and the leg never filled."
    if "primary leg did not fill" in err_blob:
        return (
            "The primary leg didn't fill in time, so the secondary leg was deliberately skipped "
            "(sequential execution mode protects against naked exposure)."
        )
    if "insufficient" in err_blob or "balance" in err_blob:
        return "Insufficient balance on one of the venues at the moment of execution."
    if "rejected" in err_blob:
        return "The venue rejected the order — see the response body in the venue-responses section."
    return "The leg(s) did not reach a filled state — see the timeline and venue responses below."


def _leg_statuses(d: TradeAnalyzerInput) -> tuple[str, str]:
    yes = no = ""
    for o in d.orders:
        side = (o.get("side") or "").lower()
        st = (o.get("status") or "").lower()
        if side == "yes" and not yes:
            yes = st
        elif side == "no" and not no:
            no = st
    return yes, no


def _leg_filled_qty(d: TradeAnalyzerInput) -> tuple[float, float]:
    yes = no = 0.0
    for o in d.orders:
        side = (o.get("side") or "").lower()
        q = _to_float(o.get("fill_qty"))
        if side == "yes":
            yes += q
        elif side == "no":
            no += q
    return yes, no


def _opp_float(opp: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    v = opp.get(key) if isinstance(opp, Mapping) else None
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _opp_int(opp: Mapping[str, Any], key: str, default: int = 0) -> int:
    v = opp.get(key) if isinstance(opp, Mapping) else None
    if v is None:
        return int(default)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return int(default)


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_ts(ts: Any) -> str:
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(ts, (int, float)) and not math.isnan(ts) and ts > 0:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts)


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def _status_emoji(status: str) -> str:
    return {
        "filled": "✅",
        "closed": "✅",
        "settled": "✅",
        "simulated": "🧪",
        "submitted": "⏳",
        "pending": "⏳",
        "recovering": "⚠️",
        "failed": "❌",
        "aborted": "❌",
    }.get((status or "").lower(), "·")


def _order_emoji(status: str, fill_qty: float, qty: float) -> str:
    if status == "filled" and fill_qty >= qty > 0:
        return "✅"
    if status == "partial" or (0 < fill_qty < qty):
        return "🟡"
    if status in {"cancelled", "aborted"}:
        return "⊘"
    if status == "failed":
        return "❌"
    if status in {"submitted", "pending"}:
        return "⏳"
    return "·"
