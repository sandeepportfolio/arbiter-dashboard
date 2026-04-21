"""validate_env.py — shape + sanity check on .env.production.

Runs BEFORE any credential handshake. Catches the most common "oops the
template placeholder is still there" and "oops you put demo URL in prod"
mistakes without ever talking to a platform.

Usage:
    set -a; source .env.production; set +a
    python scripts/setup/validate_env.py

Exit codes:
    0 — all required env vars present and sanity-check pass
    1 — one or more fatal problems (missing, placeholder, wrong URL)

NEVER prints actual secret values. Only presence, length, and prefix hints.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Tuple

# Force UTF-8 stdout so ✓ ⚠ ✗ render on Windows cmd.exe (cp1252 default).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

# (name, description, validator, fatal)
# validator returns None on pass OR an error-message string on fail.
def _starts_with(prefix: str):
    return lambda v: None if v.startswith(prefix) else f"expected to start with {prefix!r}"

def _not_placeholder(v: str) -> str | None:
    bad = ("<", "placeholder", "REPLACE", "your-", "your_", "TODO", "XXX")
    if any(tok.lower() in v.lower() for tok in bad):
        return "still contains a template placeholder"
    return None

def _min_length(n: int):
    return lambda v: None if len(v) >= n else f"too short (expected >= {n} chars, got {len(v)})"

def _not_demo(v: str) -> str | None:
    if "demo" in v.lower() or "demo-api" in v.lower():
        return "looks like a demo/sandbox value — Phase 5 requires production"
    return None

def _is_path_readable(v: str) -> str | None:
    p = Path(v)
    if not p.exists():
        return f"file not found at {v}"
    if not p.is_file():
        return f"path is not a regular file: {v}"
    try:
        with open(p, "rb") as f:
            head = f.read(64)
        if b"BEGIN" not in head:
            return "file doesn't look like a PEM (no BEGIN header in first 64 bytes)"
    except Exception as e:
        return f"cannot read file: {e}"
    return None

def _is_hex(expected_len: int | None = None):
    def _check(v: str) -> str | None:
        s = v.strip().lower()
        if s.startswith("0x"):
            s = s[2:]
        if not all(c in "0123456789abcdef" for c in s):
            return "not a hex string"
        if expected_len is not None and len(s) != expected_len:
            return f"wrong length (expected {expected_len} hex chars, got {len(s)})"
        return None
    return _check

def _bool_like(v: str) -> str | None:
    if v.strip().lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off", ""):
        return f"expected bool-like (true/false), got {v!r}"
    return None

def _int_range(lo: int, hi: int):
    def _check(v: str) -> str | None:
        try:
            n = int(v)
        except ValueError:
            return f"not an integer: {v!r}"
        if not (lo <= n <= hi):
            return f"out of range [{lo}, {hi}]: {n}"
        return None
    return _check


CHECKS: List[Tuple[str, str, list, bool]] = [
    # Core
    ("DRY_RUN",                       "live-trading flag",             [_bool_like, lambda v: None if v.strip().lower() in ("false", "0", "no", "off") else "expected false for production"], True),
    ("DATABASE_URL",                  "Postgres connection URL",       [_not_placeholder, _starts_with("postgresql://"), _min_length(30)], True),
    # Kalshi PROD (must not be demo)
    ("KALSHI_BASE_URL",               "Kalshi prod REST base URL",      [_not_placeholder, _starts_with("https://"), _not_demo], True),
    ("KALSHI_API_KEY_ID",             "Kalshi prod API key ID",         [_not_placeholder, _min_length(20)], True),
    ("KALSHI_PRIVATE_KEY_PATH",       "path to Kalshi RSA private key", [_not_placeholder, _is_path_readable], True),
    # Polymarket PROD
    ("POLYMARKET_CLOB_URL",           "Polymarket CLOB URL",            [_not_placeholder, _starts_with("https://clob.polymarket.com")], True),
    ("POLY_PRIVATE_KEY",              "Polymarket wallet private key (HEX, 64 chars, no 0x)", [_not_placeholder, _is_hex(64)], True),
    ("POLY_FUNDER",                   "Polymarket funder wallet address", [_not_placeholder, _starts_with("0x"), _is_hex(None)], True),
    ("POLY_SIGNATURE_TYPE",           "Polymarket signature type",      [_not_placeholder, _int_range(0, 4)], True),
    # Safety caps
    ("PHASE4_MAX_ORDER_USD",          "Phase 4 adapter hard-lock",      [_not_placeholder, _int_range(1, 100)], True),
    ("PHASE5_MAX_ORDER_USD",          "Phase 5 adapter hard-lock",      [_not_placeholder, _int_range(1, 100)], True),
    ("MAX_POSITION_USD",              "AutoExecutor position cap",       [_not_placeholder, _int_range(1, 100)], True),
    # Auto-execute (default off is OK; true requires explicit flip)
    ("AUTO_EXECUTE_ENABLED",          "Auto-execute toggle",            [_bool_like], False),
    # Telegram (required for Phase 5 preflight)
    ("TELEGRAM_BOT_TOKEN",            "Telegram bot token from @BotFather", [_not_placeholder, _min_length(40)], True),
    ("TELEGRAM_CHAT_ID",              "Telegram chat id from @userinfobot", [_not_placeholder, lambda v: None if v.lstrip('-').isdigit() else "expected numeric chat id"], True),
    # Preflight acks
    ("POLYMARKET_MIGRATION_ACK",      "Polymarket migration ack (preflight #13)", [_not_placeholder, lambda v: None if v.strip() == "ACKNOWLEDGED" else 'must equal "ACKNOWLEDGED"'], True),
    ("OPERATOR_RUNBOOK_ACK",          "Operator runbook ack (preflight #15)", [_not_placeholder, lambda v: None if v.strip() == "ACKNOWLEDGED" else 'must equal "ACKNOWLEDGED"'], True),
    # UI auth
    ("UI_SESSION_SECRET",             "HMAC session secret",            [_not_placeholder, _is_hex(64)], True),
]


def _render_reason(value: str, reasons: list[str]) -> str:
    masked = _mask(value)
    return f"  value={masked}  reason(s): {'; '.join(reasons)}"


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "<" + "*" * len(value) + ">"
    return f"<{value[:3]}...{value[-3:]} len={len(value)}>"


def main() -> int:
    print("== Arbiter .env.production validator ==")
    env_file = Path(".env.production")
    if env_file.exists():
        print(f"  found {env_file} (size={env_file.stat().st_size} bytes)")
    else:
        print(f"  note: {env_file} not found; reading directly from os.environ")

    passed, failed, warnings = [], [], []

    for name, desc, validators, fatal in CHECKS:
        raw = os.environ.get(name, "")
        if not raw:
            if fatal:
                failed.append((name, desc, ["not set in environment"]))
            else:
                warnings.append((name, desc, ["not set (optional)"]))
            continue
        reasons = [msg for v in validators if (msg := v(raw)) is not None]
        if reasons:
            if fatal:
                failed.append((name, desc, reasons))
            else:
                warnings.append((name, desc, reasons))
        else:
            passed.append((name, desc))

    print(f"\nPASS: {len(passed)}")
    for name, desc in passed:
        print(f"  ✓  {name:<28} {desc}")

    if warnings:
        print(f"\nWARN: {len(warnings)}")
        for name, desc, reasons in warnings:
            print(f"  ⚠  {name:<28} {desc}")
            print(_render_reason(os.environ.get(name, ""), reasons))

    if failed:
        print(f"\nFAIL: {len(failed)}")
        for name, desc, reasons in failed:
            print(f"  ✗  {name:<28} {desc}")
            print(_render_reason(os.environ.get(name, ""), reasons))
        print("\n.env.production validation FAILED — fix the FAIL rows above and re-run.")
        return 1

    print("\n✓ .env.production shape + sanity OK — proceed to live platform checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
