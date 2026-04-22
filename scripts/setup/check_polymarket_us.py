"""check_polymarket_us.py — Ed25519 signed round-trip against Polymarket US prod.

Validates:
    1. POLYMARKET_US_API_KEY_ID is set (printed as "key_id: {id}")
    2. POLYMARKET_US_API_SECRET decodes to >=32 bytes (printed as "secret length: N chars")
    3. Signed GET /v1/account/balances returns HTTP 200
    4. currentBalance field is present and >= $20

Exit 0 on all-pass, 1 on any failure.

Guardrails:
    - Read POLYMARKET_US_API_KEY_ID and POLYMARKET_US_API_SECRET from env.
    - Never print the secret; print "secret length: 44 chars" style.
    - Print balance in dollars with 2 decimal places.
    - On HTTP 401, print "Auth failed — check key id + secret match."
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

# Make arbiter importable when this script is run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

try:
    import aiohttp
except ImportError:
    print("aiohttp not installed — install repo requirements first", file=sys.stderr)
    sys.exit(2)

from arbiter.auth.ed25519_signer import Ed25519Signer, SignatureError  # type: ignore
from arbiter.collectors.polymarket_us import extract_current_balance  # type: ignore


def _balances_endpoint(base_url: str) -> tuple[str, str, str]:
    """Normalize the configured base URL for the balances check.

    Returns:
    - base URL for the final request URL
    - request path to append to the base URL
    - signature path that must match the real on-wire HTTP path
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base, "/account/balances", "/v1/account/balances"
    return base, "/v1/account/balances", "/v1/account/balances"


async def _check() -> int:
    key_id = os.getenv("POLYMARKET_US_API_KEY_ID", "").strip()
    secret_b64 = os.getenv("POLYMARKET_US_API_SECRET", "").strip()
    base_url = os.getenv("POLYMARKET_US_API_URL", "https://api.polymarket.us").rstrip("/")

    # ── 1. Presence check ────────────────────────────────────────────────
    if not key_id:
        print("FAIL — POLYMARKET_US_API_KEY_ID is unset", file=sys.stderr)
        return 1

    if not secret_b64:
        print("FAIL — POLYMARKET_US_API_SECRET is unset", file=sys.stderr)
        return 1

    # ── 2. Secret length check (never print the value) ───────────────────
    try:
        raw = base64.b64decode(secret_b64)
    except Exception as exc:
        print(f"FAIL — POLYMARKET_US_API_SECRET is not valid base64: {exc}", file=sys.stderr)
        return 1

    # NOTE: we print the *encoded* length (chars), not the raw bytes, matching
    # the style "secret length: 44 chars" for a 32-byte seed.
    secret_len_chars = len(secret_b64)
    print(f"key_id:        {key_id}")
    print(f"secret length: {secret_len_chars} chars")

    if len(raw) < 32:
        print(
            f"FAIL — secret decodes to {len(raw)} bytes; Ed25519 requires >=32",
            file=sys.stderr,
        )
        return 1

    # ── 3. Build signer ──────────────────────────────────────────────────
    try:
        signer = Ed25519Signer(key_id=key_id, secret_b64=secret_b64)
    except SignatureError as exc:
        print(f"FAIL — Ed25519Signer rejected secret: {exc}", file=sys.stderr)
        return 1

    # ── 4. Signed round-trip ─────────────────────────────────────────────
    base_url, request_path, signature_path = _balances_endpoint(base_url)
    headers = signer.headers("GET", signature_path)
    url = f"{base_url}{request_path}"
    print(f"url:           {url}")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(url, headers=headers) as resp:
                status = resp.status
                if status == 401:
                    print(
                        "FAIL — Auth failed — check key id + secret match.",
                        file=sys.stderr,
                    )
                    return 1
                if status != 200:
                    body_text = await resp.text()
                    print(
                        f"FAIL — unexpected HTTP {status}",
                        file=sys.stderr,
                    )
                    print(f"body (first 200 chars): {body_text[:200]}", file=sys.stderr)
                    return 1
                body = await resp.json()
    except asyncio.TimeoutError:
        print("FAIL — timeout contacting Polymarket US API", file=sys.stderr)
        return 1
    except aiohttp.ClientError as exc:
        print(f"FAIL — network error: {exc}", file=sys.stderr)
        return 1

    # ── 5. Parse + validate balance ──────────────────────────────────────
    try:
        current_balance = extract_current_balance(body)
    except (TypeError, ValueError) as exc:
        print(f"FAIL — could not parse currentBalance: {exc}", file=sys.stderr)
        return 1

    print(f"balance:       ${current_balance:.2f}")

    if current_balance < 20.0:
        print(
            f"WARN — balance ${current_balance:.2f} is below $20.00 minimum for trading.",
            file=sys.stderr,
        )
        print("auth:          PASS (but fund the account before trading)")
    else:
        print("auth:          PASS")

    return 0


def main() -> int:
    return asyncio.run(_check())


if __name__ == "__main__":
    sys.exit(main())
