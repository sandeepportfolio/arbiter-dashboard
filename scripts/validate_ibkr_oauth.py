#!/usr/bin/env python3
"""Live validation for the IBKR OAuth 1.0a (Path B, zero-login) cutover.

Run this ONCE after IBKR issues the consumer key + access token and the
values are set in the environment (.env.production):

    IBKR_OAUTH_CONSUMER_KEY / IBKR_OAUTH_ACCESS_TOKEN /
    IBKR_OAUTH_ACCESS_TOKEN_SECRET (+ the three key-file paths)

It proves the full chain against the real api.ibkr.com:
  1. DH handshake → Live Session Token computed
  2. LST verifies against the server's HMAC signature
  3. A signed read (GET /portfolio/accounts) returns 200

All three green → set IBKR_OAUTH_ENABLED=true and restart; the bot then
runs without the local gateway and without any browser login, forever.

Usage:
    set -a; source .env.production; set +a
    python3 scripts/validate_ibkr_oauth.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from arbiter.auth.ibkr_oauth import IbkrOAuth1a  # noqa: E402
from arbiter.config.settings import ForecastExConfig  # noqa: E402


async def main() -> int:
    cfg = ForecastExConfig()
    if not cfg.oauth_configured:
        print("✗ OAuth material incomplete — need IBKR_OAUTH_CONSUMER_KEY, "
              "IBKR_OAUTH_ACCESS_TOKEN, IBKR_OAUTH_ACCESS_TOKEN_SECRET and "
              "the three key-file paths set in the environment.")
        return 2

    oauth = IbkrOAuth1a.from_config(cfg)
    print(f"consumer key: {cfg.oauth_consumer_key!r}  realm: {oauth.realm}  "
          f"base: {oauth.api_base}")

    async with aiohttp.ClientSession() as session:
        # 1+2: handshake + signature validation (raises when either fails).
        try:
            await oauth.ensure_live_session_token(session)
        except Exception as exc:  # noqa: BLE001 — report, don't trace-dump
            print(f"✗ LST handshake failed: {exc}")
            return 1
        print("✓ live session token computed and signature-verified "
              f"(valid ≈22h, expires ts={oauth._lst_expires:.0f})")

        # 3: signed read.
        url = f"{oauth.api_base}/portfolio/accounts"
        headers = {
            "Authorization": oauth.auth_header("GET", url),
            "Accept": "application/json",
            "User-Agent": "arbiter-ibkr-oauth/1.0",
        }
        async with session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status == 200:
                print(f"✓ signed GET /portfolio/accounts → 200: {body[:200]}")
            else:
                print(f"✗ signed GET /portfolio/accounts → {resp.status}: "
                      f"{body[:300]}")
                return 1

    print("\nAll green — set IBKR_OAUTH_ENABLED=true in .env.production, "
          "rebuild/restart, and retire the browser login for good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
