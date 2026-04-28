#!/usr/bin/env python3
"""
Batch Discovery + Confirmation Pipeline
========================================
Discovers ALL cross-platform market pairs between Kalshi and Polymarket US,
verifies them via the LLM verifier, and auto-confirms + enables auto-trade
for high-confidence pairs.

Usage (from inside Docker container):
    python -m scripts.batch_discover_and_confirm

Or via the API:
    POST /api/batch-discover

This replaces the manual one-at-a-time confirmation workflow.
"""
import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import date, datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("batch_confirm")

BASE = os.environ.get("ARBITER_API_URL", "http://localhost:8080")
AUTH_TOKEN = None  # Will be set after login


def _login():
    """Authenticate and get a bearer token."""
    global AUTH_TOKEN
    body = json.dumps({
        "email": os.environ.get("OPS_EMAIL", "sparx.sandeep@gmail.com"),
        "password": os.environ.get("OPS_PASSWORD", "saibaba1"),
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            AUTH_TOKEN = data.get("token") or data.get("access_token")
            logger.info("Authenticated successfully")
    except Exception as e:
        logger.warning("Auth failed (may not be required): %s", e)


def _api(method, path, data=None):
    """Make an authenticated API call."""
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fetch(path):
    """GET request."""
    return _api("GET", path)


def _post(path, data):
    """POST request."""
    return _api("POST", path, data)


def fetch_all_candidates():
    """Get all candidate mappings from the DB."""
    # The API paginates at 500, so we need to get candidates directly
    candidates = []
    try:
        # Try the market-mappings endpoint with status filter
        data = _fetch("/api/market-mappings?status=candidate&limit=10000")
        if isinstance(data, list):
            candidates = data
        logger.info("Fetched %d candidates from API", len(candidates))
    except Exception as e:
        logger.error("Failed to fetch candidates: %s", e)
    return candidates


def fetch_prices():
    """Get current price data for all markets."""
    try:
        return _fetch("/api/prices")
    except Exception as e:
        logger.error("Failed to fetch prices: %s", e)
        return {}


def has_both_prices(canonical_id, prices):
    """Check if a market has prices from both platforms."""
    kalshi_key = f"price:kalshi:{canonical_id}"
    poly_key = f"price:polymarket:{canonical_id}"
    return kalshi_key in prices and poly_key in prices


def compute_gross_edge(canonical_id, prices):
    """Compute the gross edge for a market pair."""
    kalshi_key = f"price:kalshi:{canonical_id}"
    poly_key = f"price:polymarket:{canonical_id}"
    k = prices.get(kalshi_key, {})
    p = prices.get(poly_key, {})

    if not k or not p:
        return 0.0, None

    # Try both directions
    edge1 = 1.0 - k.get("yes_price", 1) - p.get("no_price", 1)  # K-YES + P-NO
    edge2 = 1.0 - p.get("yes_price", 1) - k.get("no_price", 1)  # P-YES + K-NO
    best = max(edge1, edge2)
    direction = "k_yes_p_no" if edge1 >= edge2 else "p_yes_k_no"
    return best, direction


def llm_verify_via_api(kalshi_q, poly_q):
    """Call the LLM verifier via the Arbiter API (if available) or HTTP sidecar."""
    verifier_url = os.environ.get("LLM_VERIFIER_HTTP_URL", "http://host.docker.internal:8079/verify")
    try:
        body = json.dumps({
            "kalshi_question": kalshi_q,
            "polymarket_question": poly_q,
        }).encode()
        req = urllib.request.Request(
            verifier_url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            return result.get("answer", "MAYBE").upper()
    except Exception as e:
        logger.warning("LLM verify failed: %s", e)
        return "MAYBE"


def confirm_and_enable(canonical_id, note, resolution_criteria=None):
    """Confirm a mapping and enable auto-trade in one flow."""
    # Step 1: Confirm
    payload = {
        "action": "confirm",
        "note": note,
        "resolution_match_status": "identical",
    }
    if resolution_criteria:
        payload["resolution_criteria"] = resolution_criteria
    try:
        _post(f"/api/market-mappings/{canonical_id}", payload)
    except Exception as e:
        logger.error("Failed to confirm %s: %s", canonical_id, e)
        return False

    # Step 2: Enable auto-trade
    try:
        _post(f"/api/market-mappings/{canonical_id}", {
            "action": "enable_auto_trade",
            "note": "Auto-trade enabled by batch pipeline",
            "resolution_match_status": "identical",
        })
    except Exception as e:
        logger.error("Failed to enable auto-trade for %s: %s", canonical_id, e)
        return False

    return True


def main():
    logger.info("=" * 70)
    logger.info("BATCH DISCOVERY + CONFIRMATION PIPELINE — %s", datetime.now().isoformat())
    logger.info("=" * 70)

    # Authenticate
    _login()

    # Step 1: Get all candidate mappings
    logger.info("\n--- STEP 1: Fetching candidates ---")
    candidates = fetch_all_candidates()
    if not candidates:
        logger.info("No candidates found. Run discovery first.")
        return

    # Step 2: Get prices to check which have active markets
    logger.info("\n--- STEP 2: Fetching prices ---")
    prices = fetch_prices()
    logger.info("Total price entries: %d", len(prices))

    # Step 3: Filter candidates that have prices on both platforms
    logger.info("\n--- STEP 3: Filtering for dual-platform coverage ---")
    viable = []
    for c in candidates:
        cid = c.get("canonical_id", "")
        if has_both_prices(cid, prices):
            edge, direction = compute_gross_edge(cid, prices)
            viable.append({**c, "gross_edge": edge, "direction": direction})

    viable.sort(key=lambda x: -x.get("gross_edge", 0))
    logger.info("Candidates with both platforms: %d / %d", len(viable), len(candidates))

    # Step 4: LLM-verify and confirm high-confidence pairs
    logger.info("\n--- STEP 4: LLM verification + confirmation ---")
    confirmed_count = 0
    skipped_count = 0
    failed_count = 0

    for c in viable:
        cid = c.get("canonical_id", "")
        score = c.get("mapping_score", 0) or c.get("confidence", 0)
        kalshi_id = c.get("kalshi", "") or c.get("kalshi_market_id", "")
        poly_slug = c.get("polymarket", "") or c.get("polymarket_slug", "")
        desc = c.get("description", "")[:60]
        edge = c.get("gross_edge", 0)

        # Skip if already confirmed/rejected
        status = c.get("status", "candidate")
        if status in ("confirmed", "rejected"):
            continue

        # Skip if score is too low (below 0.50 means very weak match)
        if score < 0.40:
            skipped_count += 1
            continue

        # Skip if no market IDs
        if not kalshi_id or not poly_slug:
            skipped_count += 1
            continue

        logger.info("Verifying: %s (score=%.3f edge=%.2fc)", cid[:40], score, edge * 100)

        # Get the market descriptions for LLM verification
        kalshi_q = desc  # Use the description as the question
        poly_q = c.get("polymarket_question", "") or desc

        # Call LLM verifier
        llm_result = llm_verify_via_api(kalshi_q, poly_q)
        logger.info("  LLM result: %s", llm_result)

        if llm_result == "YES":
            # Auto-confirm
            success = confirm_and_enable(
                cid,
                note=f"Batch-confirmed: LLM=YES, score={score:.3f}, edge={edge*100:.1f}c",
                resolution_criteria={
                    "criteria_match": "identical",
                    "verified_by": "batch_pipeline",
                    "verification_date": datetime.now().isoformat(),
                    "llm_result": "YES",
                    "score": score,
                },
            )
            if success:
                confirmed_count += 1
                logger.info("  CONFIRMED + AUTO-TRADE ENABLED: %s", cid)
            else:
                failed_count += 1
        elif llm_result == "NO":
            # Reject to prevent re-checking
            try:
                _post(f"/api/market-mappings/{cid}", {
                    "action": "review",
                    "note": f"Batch-rejected: LLM=NO, score={score:.3f}",
                })
            except Exception:
                pass
            skipped_count += 1
        else:
            # MAYBE — skip for now, don't reject
            skipped_count += 1

    logger.info("\n--- RESULTS ---")
    logger.info("Confirmed: %d", confirmed_count)
    logger.info("Skipped: %d", skipped_count)
    logger.info("Failed: %d", failed_count)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
