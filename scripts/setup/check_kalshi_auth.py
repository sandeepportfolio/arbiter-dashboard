"""check_kalshi_auth.py — round-trip authenticate against Kalshi prod.

Runs signed GET /portfolio/balance against KALSHI_BASE_URL with
KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH. Prints:

    balance: $N.NN
    url: https://api.elections.kalshi.com/trade-api/v2 (or demo)
    auth: PASS | FAIL

Exit 0 on PASS (HTTP 200 + parseable balance), 1 on any failure.

NEVER prints the private key or API key ID.
"""
from __future__ import annotations

import asyncio
import json
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

from urllib.parse import urlparse

from arbiter.collectors.kalshi import KalshiAuth  # type: ignore


async def _check() -> int:
    base = os.getenv("KALSHI_BASE_URL", "").rstrip("/")
    key_id = os.getenv("KALSHI_API_KEY_ID", "")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not base or not key_id or not key_path:
        print("FAIL — KALSHI_BASE_URL / KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH must all be set")
        return 1

    if "demo" in base.lower():
        print(
            f"FAIL — KALSHI_BASE_URL points at demo ({base}).\n"
            "Phase 5 requires PRODUCTION: https://api.elections.kalshi.com/trade-api/v2"
        )
        return 1

    if not Path(key_path).exists():
        print(f"FAIL — private key not found at {key_path}")
        return 1

    auth = KalshiAuth(key_id, key_path)
    if not auth.is_authenticated:
        print("FAIL — KalshiAuth.is_authenticated is False (check PEM format + api_key_id)")
        return 1

    sign_prefix = urlparse(base).path  # e.g. "/trade-api/v2"
    signed_path = f"{sign_prefix}/portfolio/balance"
    headers = auth.get_headers("GET", signed_path)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{base}/portfolio/balance", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                status = resp.status
                body_text = await resp.text()
        except asyncio.TimeoutError:
            print("FAIL — timeout contacting Kalshi")
            return 1
        except aiohttp.ClientError as e:
            print(f"FAIL — Kalshi client error: {e}")
            return 1

    print(f"url:       {base}")
    print(f"status:    HTTP {status}")

    if status == 200:
        try:
            body = json.loads(body_text)
            balance_cents = int(body.get("balance", 0))
            print(f"balance:   ${balance_cents / 100:.2f}")
            if balance_cents < 100:
                print(
                    "WARN — balance is less than $1.00. Phase 5 first-live-trade will likely fail with "
                    "insufficient funds. Recommend funding to at least $100 before flipping AUTO_EXECUTE_ENABLED=true."
                )
            print("auth:      PASS")
            return 0
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"FAIL — could not parse balance response: {e}")
            print(f"body (first 200 chars): {body_text[:200]}")
            return 1
    elif status == 401:
        print("FAIL — HTTP 401 INCORRECT_API_KEY_SIGNATURE")
        print("  Common causes:")
        print("  - KALSHI_API_KEY_ID doesn't match the PEM on disk (did you mix up demo and prod keys?)")
        print("  - PEM was corrupted during download (re-download from Kalshi portal)")
        print("  - Clock skew > 30s from UTC (check `timedatectl status`)")
        return 1
    else:
        print(f"FAIL — unexpected status {status}")
        print(f"body: {body_text[:200]}")
        return 1


def main() -> int:
    return asyncio.run(_check())


if __name__ == "__main__":
    sys.exit(main())
