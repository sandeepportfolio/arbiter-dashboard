#!/usr/bin/env python3
"""
Validation Agent 6: Validate market mappings batch 6 (indices 500-625)
Hits both Kalshi and Polymarket APIs to verify market existence and validity.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("validate_batch6")

SEEDS_PATH = REPO_ROOT / "arbiter/mapping/fixtures/market_seeds_auto.json"
BATCH_START = 500
BATCH_END = 625  # exclusive

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Load .env
def load_dotenv():
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

load_dotenv()

# ---------------------------------------------------------------------------
# Kalshi auth
# ---------------------------------------------------------------------------
class KalshiAuth:
    def __init__(self):
        self.api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private.pem")
        if key_path.startswith("./"):
            key_path = str(REPO_ROOT / key_path[2:])
        self._private_key = None
        if key_path and Path(key_path).exists():
            try:
                with open(key_path, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(f.read(), password=None)
                log.info("Kalshi private key loaded from %s", key_path)
            except Exception as e:
                log.warning("Could not load Kalshi private key: %s", e)

    @property
    def is_authenticated(self) -> bool:
        return bool(self._private_key and self.api_key_id)

    def get_headers(self, method: str, path: str) -> dict:
        if not self.is_authenticated:
            return {"Accept": "application/json"}
        ts = int(time.time() * 1000)
        message = f"{ts}{method}{path}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "Accept": "application/json",
        }


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    index: int
    canonical_id: str
    kalshi_ticker: str
    polymarket_slug: str
    resolution_date: str

    # Kalshi check
    kalshi_exists: bool = False
    kalshi_status: str = ""
    kalshi_close_time: str = ""
    kalshi_title: str = ""
    kalshi_error: str = ""

    # Polymarket check
    poly_exists: bool = False
    poly_active: bool = False
    poly_end_date: str = ""
    poly_title: str = ""
    poly_error: str = ""

    # Overall
    is_valid: bool = False
    invalid_reasons: list = field(default_factory=list)

    def evaluate(self):
        reasons = []
        if not self.kalshi_exists:
            reasons.append(f"kalshi_not_found: {self.kalshi_error or 'no data'}")
        if not self.poly_exists:
            reasons.append(f"poly_not_found: {self.poly_error or 'no data'}")

        # Check resolution date alignment
        if self.kalshi_exists and self.kalshi_close_time:
            try:
                k_date = self.kalshi_close_time[:10]  # YYYY-MM-DD prefix
                r_date = self.resolution_date
                if k_date and r_date and abs(
                    (datetime.fromisoformat(k_date) - datetime.fromisoformat(r_date)).days
                ) > 7:
                    reasons.append(f"expiry_mismatch: kalshi={k_date} vs seed={r_date}")
            except Exception:
                pass

        # Check if market is expired (resolved before today)
        today = datetime.now(timezone.utc).date()
        try:
            res_date = datetime.fromisoformat(self.resolution_date).date()
            if res_date < today:
                # It's past - that's okay if both platforms show it closed/resolved
                if self.kalshi_status and self.kalshi_status not in ("finalized", "settled", "determined"):
                    pass  # Don't invalidate - many markets resolve at stated date
        except Exception:
            pass

        self.invalid_reasons = reasons
        self.is_valid = len(reasons) == 0


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
async def validate_kalshi(session: aiohttp.ClientSession, auth: KalshiAuth, ticker: str) -> dict:
    """Fetch a single Kalshi market by ticker."""
    path = f"/trade-api/v2/markets/{ticker}"
    url = f"{KALSHI_BASE}/markets/{ticker}"
    headers = auth.get_headers("GET", path)
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 404:
                return {"found": False, "error": "404 not found"}
            if resp.status == 429:
                return {"found": False, "error": "rate_limited"}
            if resp.status != 200:
                return {"found": False, "error": f"http_{resp.status}"}
            data = await resp.json()
            market = data.get("market") or data
            return {
                "found": True,
                "status": market.get("status", ""),
                "close_time": market.get("close_time") or market.get("expiration_time", ""),
                "title": market.get("title", ""),
            }
    except asyncio.TimeoutError:
        return {"found": False, "error": "timeout"}
    except Exception as e:
        return {"found": False, "error": str(e)[:80]}


async def validate_polymarket(session: aiohttp.ClientSession, slug: str) -> dict:
    """Check Polymarket gamma API for a market by slug."""
    # Try events endpoint first
    try:
        async with session.get(
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 429:
                await asyncio.sleep(2)
                return {"found": False, "error": "rate_limited"}
            if resp.status == 200:
                data = await resp.json()
                events = data if isinstance(data, list) else data.get("events", [])
                if events:
                    ev = events[0]
                    markets = ev.get("markets", [])
                    active = any(not m.get("closed", True) for m in markets)
                    end_date = ev.get("endDate") or ev.get("end_date") or ""
                    if end_date and len(end_date) > 10:
                        end_date = end_date[:10]
                    return {
                        "found": True,
                        "active": active,
                        "end_date": end_date,
                        "title": ev.get("title", ""),
                        "source": "events",
                    }
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        log.debug("Gamma events error for %s: %s", slug, e)

    # Fall back to markets endpoint
    try:
        async with session.get(
            f"{GAMMA_BASE}/markets",
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                if markets:
                    m = markets[0]
                    end_date = m.get("endDate") or m.get("end_date") or ""
                    if end_date and len(end_date) > 10:
                        end_date = end_date[:10]
                    return {
                        "found": True,
                        "active": not m.get("closed", False) and not m.get("resolved", False),
                        "end_date": end_date,
                        "title": m.get("question", m.get("title", "")),
                        "source": "markets",
                    }
    except asyncio.TimeoutError:
        return {"found": False, "error": "timeout"}
    except Exception as e:
        return {"found": False, "error": str(e)[:80]}

    return {"found": False, "error": "not_found_on_gamma"}


# ---------------------------------------------------------------------------
# Main validation loop
# ---------------------------------------------------------------------------
async def validate_batch(seeds: list) -> list[ValidationResult]:
    auth = KalshiAuth()
    results = []

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent requests per platform

        async def validate_one(idx: int, seed: dict) -> ValidationResult:
            async with semaphore:
                result = ValidationResult(
                    index=idx,
                    canonical_id=seed.get("canonical_id", ""),
                    kalshi_ticker=seed.get("kalshi", ""),
                    polymarket_slug=seed.get("polymarket", ""),
                    resolution_date=seed.get("resolution_date", ""),
                )

                # Kalshi check
                k = await validate_kalshi(session, auth, result.kalshi_ticker)
                result.kalshi_exists = k.get("found", False)
                result.kalshi_status = k.get("status", "")
                result.kalshi_close_time = k.get("close_time", "")
                result.kalshi_title = k.get("title", "")
                result.kalshi_error = k.get("error", "")

                # Small delay to be polite to Kalshi
                await asyncio.sleep(0.1)

                # Polymarket check
                p = await validate_polymarket(session, result.polymarket_slug)
                result.poly_exists = p.get("found", False)
                result.poly_active = p.get("active", False)
                result.poly_end_date = p.get("end_date", "")
                result.poly_title = p.get("title", "")
                result.poly_error = p.get("error", "")

                result.evaluate()

                status = "VALID" if result.is_valid else "INVALID"
                log.info(
                    "[%d] %s | k=%s p=%s | %s",
                    idx,
                    result.kalshi_ticker,
                    "OK" if result.kalshi_exists else "MISS",
                    "OK" if result.poly_exists else "MISS",
                    status,
                )
                if not result.is_valid:
                    log.debug("  Reasons: %s", result.invalid_reasons)

                return result

        batch = seeds[BATCH_START:BATCH_END]
        tasks = [validate_one(BATCH_START + i, seed) for i, seed in enumerate(batch)]

        # Run in chunks to avoid overwhelming APIs
        chunk_size = 20
        for i in range(0, len(tasks), chunk_size):
            chunk = tasks[i:i + chunk_size]
            chunk_results = await asyncio.gather(*chunk, return_exceptions=True)
            for r in chunk_results:
                if isinstance(r, Exception):
                    log.error("Task failed: %s", r)
                else:
                    results.append(r)
            if i + chunk_size < len(tasks):
                log.info("Chunk %d/%d done, pausing 1s...", i // chunk_size + 1, (len(tasks) + chunk_size - 1) // chunk_size)
                await asyncio.sleep(1)

    return results


def update_seeds(seeds: list, results: list[ValidationResult]) -> tuple[int, int]:
    confirmed = 0
    unchanged = 0
    for r in results:
        if r.is_valid:
            seeds[r.index]["status"] = "confirmed"
            confirmed += 1
        else:
            unchanged += 1
    return confirmed, unchanged


def write_report(results: list[ValidationResult], confirmed: int, unchanged: int) -> str:
    total = len(results)
    valid_results = [r for r in results if r.is_valid]
    invalid_results = [r for r in results if not r.is_valid]

    kalshi_miss = [r for r in results if not r.kalshi_exists]
    poly_miss = [r for r in results if not r.poly_exists]

    # Collect unique error reasons
    from collections import Counter
    reason_counts = Counter()
    for r in invalid_results:
        for reason in r.invalid_reasons:
            key = reason.split(":")[0]
            reason_counts[key] += 1

    lines = [
        "# Validation Report — Batch 6 (Indices 500–624)",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        f"- Total checked: {total}",
        f"- **Confirmed (valid):** {confirmed}",
        f"- **Invalid/unchanged:** {unchanged}",
        f"- Kalshi not found: {len(kalshi_miss)}",
        f"- Polymarket not found: {len(poly_miss)}",
        "",
        "## Failure Breakdown",
    ]
    for reason, count in reason_counts.most_common():
        lines.append(f"- `{reason}`: {count}")

    if invalid_results:
        lines += ["", "## Invalid Mappings (sample, max 30)"]
        for r in invalid_results[:30]:
            lines.append(f"- [{r.index}] `{r.kalshi_ticker}` / `{r.polymarket_slug}`")
            for reason in r.invalid_reasons:
                lines.append(f"  - {reason}")

    if valid_results:
        lines += ["", "## Valid Mappings (confirmed)", f"Total: {len(valid_results)}"]

    return "\n".join(lines)


async def main():
    log.info("Loading market seeds from %s", SEEDS_PATH)
    with open(SEEDS_PATH) as f:
        seeds = json.load(f)

    log.info("Total seeds: %d. Validating batch [%d:%d] (%d mappings).",
             len(seeds), BATCH_START, BATCH_END, BATCH_END - BATCH_START)

    results = await validate_batch(seeds)
    log.info("Validation complete. %d results.", len(results))

    confirmed, unchanged = update_seeds(seeds, results)
    log.info("Confirmed: %d | Unchanged (invalid): %d", confirmed, unchanged)

    # Write updated seeds
    with open(SEEDS_PATH, "w") as f:
        json.dump(seeds, f, indent=2)
    log.info("Updated seeds written to %s", SEEDS_PATH)

    # Write report
    report = write_report(results, confirmed, unchanged)
    report_path = REPO_ROOT / "data/validation_report_batch6.md"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(report)
    log.info("Report written to %s", report_path)

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
