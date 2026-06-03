"""Shared Telegram formatting utilities.

Provides clean number formatting, visual dividers, and HTML helpers
used across all alert templates. Keeps individual alert modules DRY.
"""
from __future__ import annotations

import html


# ── Visual constants ────────────────────────────────────────────────
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
THIN_DIVIDER = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"


def h(text: object) -> str:
    """HTML-escape any value for safe Telegram embedding."""
    return html.escape(str(text or ""), quote=False)


def usd(value: float, *, signed: bool = False, precision: int = 2) -> str:
    """Format a USD value cleanly: $1.23, +$0.45, -$0.12.

    Never shows trailing garbage like 0.45000001.
    """
    v = round(float(value or 0.0), precision)
    fmt = f".{precision}f"
    if signed:
        return f"${v:+{fmt}}" if v != 0 else "$0.00"
    return f"${v:{fmt}}"


def cents(value: float, precision: int = 1) -> str:
    """Format a cent value: 3.2c."""
    return f"{round(float(value or 0.0), precision):.{precision}f}c"


def pct(value: float, precision: int = 0) -> str:
    """Format a percentage: 85%."""
    return f"{round(float(value or 0.0) * 100, precision):.{precision}f}%"


def price(value: float) -> str:
    """Format a market price: $0.45 (2 decimal places for display)."""
    v = round(float(value or 0.0), 2)
    return f"${v:.2f}"


def price3(value: float) -> str:
    """Format a market price with 3 decimals: $0.452."""
    v = round(float(value or 0.0), 3)
    return f"${v:.3f}"


def qty(value: int) -> str:
    """Format a quantity with x prefix: x10."""
    return f"x{int(value or 0)}"


def age(seconds: float) -> str:
    """Format quote age: 5s, 1m 30s."""
    s = int(float(seconds or 0))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def truncate(text: str, limit: int = 80) -> str:
    """Truncate text with ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def short_id(value: str, head: int = 8, tail: int = 4) -> str:
    """Shorten long IDs (Polymarket tokens) for display."""
    value = (value or "").strip()
    if len(value) <= head + tail + 1:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def platform(name: str) -> str:
    """Uppercase platform name for display."""
    return (name or "unknown").upper()


def section(title: str, *lines: str) -> str:
    """Build a visual section with title and indented lines."""
    parts = [f"<b>{h(title)}</b>"]
    for line in lines:
        if line:
            parts.append(f"  {line}")
    return "\n".join(parts)
