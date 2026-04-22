"""
Embedding-based market discovery pipeline.

Fetches all active markets from Kalshi and Polymarket, generates sentence
embeddings, computes cosine similarity for every cross-platform pair, and
outputs matches above a configurable threshold.

Runs standalone as a batch script:
    python -m arbiter.mapping.embedding_discovery
    python -m arbiter.mapping.embedding_discovery --threshold 0.70 --output data/
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

logger = logging.getLogger("arbiter.mapping.embedding_discovery")


# ── API helpers ───────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "arbiter-discovery/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_kalshi_markets() -> list[dict[str, Any]]:
    """Paginate all open Kalshi markets via public endpoint."""
    base = "https://api.elections.kalshi.com/trade-api/v2/markets"
    markets: list[dict[str, Any]] = []
    cursor = None
    page = 0
    while True:
        params: dict[str, Any] = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        url = base + "?" + urllib.parse.urlencode(params)
        logger.info("Kalshi page %d — %s", page, url)
        try:
            data = _get_json(url)
        except Exception as exc:
            logger.warning("Kalshi fetch error: %s", exc)
            break
        batch = data.get("markets") or []
        markets.extend(batch)
        logger.info("  got %d markets (total so far: %d)", len(batch), len(markets))
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        page += 1
        time.sleep(0.3)
    return markets


def fetch_polymarket_markets() -> list[dict[str, Any]]:
    """Paginate all active Polymarket markets via Gamma API."""
    base = "https://gamma-api.polymarket.com/markets"
    markets: list[dict[str, Any]] = []
    offset = 0
    limit = 100
    page = 0
    while True:
        params = {"closed": "false", "limit": limit, "offset": offset}
        url = base + "?" + urllib.parse.urlencode(params)
        logger.info("Polymarket page %d — %s", page, url)
        try:
            data = _get_json(url)
        except Exception as exc:
            logger.warning("Polymarket fetch error: %s", exc)
            break
        batch = data if isinstance(data, list) else (data.get("markets") or [])
        markets.extend(batch)
        logger.info("  got %d markets (total so far: %d)", len(batch), len(markets))
        if len(batch) < limit:
            break
        offset += limit
        page += 1
        time.sleep(0.2)
    return markets


# ── Text extraction ───────────────────────────────────────────────────────────

def _kalshi_title(m: dict[str, Any]) -> str:
    for field in ("title", "subtitle", "yes_sub_title"):
        v = str(m.get(field) or "").strip()
        if v:
            return v
    return str(m.get("ticker") or "").strip()


def _poly_title(m: dict[str, Any]) -> str:
    for field in ("question", "title"):
        v = str(m.get(field) or "").strip()
        if v:
            return v
    return str(m.get("slug") or "").strip()


def _poly_category(m: dict[str, Any]) -> str:
    return str(m.get("category") or m.get("groupItemTitle") or "").strip()


def _kalshi_expiry(m: dict[str, Any]) -> str:
    for field in ("close_time", "expiration_time", "expected_expiration_time"):
        v = str(m.get(field) or "").strip()
        if v:
            return v
    return ""


def _poly_end_date(m: dict[str, Any]) -> str:
    for field in ("closeTime", "endDate", "end_date_iso"):
        v = str(m.get(field) or "").strip()
        if v:
            return v
    return ""


# ── Embedding + similarity ────────────────────────────────────────────────────

def build_embeddings(texts: list[str], model_name: str = "all-MiniLM-L6-v2", device: str = "cpu"):
    """Return numpy array of shape (N, dim) using sentence-transformers."""
    from sentence_transformers import SentenceTransformer  # type: ignore
    logger.info("Loading embedding model %s on %s …", model_name, device)
    model = SentenceTransformer(model_name, device=device)
    logger.info("Encoding %d texts …", len(texts))
    embeddings = model.encode(
        texts,
        batch_size=512,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return embeddings


def find_matches_chunked(
    k_embeddings,
    p_embeddings,
    kalshi_valid: list,
    poly_valid: list,
    threshold: float,
    chunk_size: int = 500,
) -> list[dict[str, Any]]:
    """
    Chunked cosine similarity to avoid materialising the full N×M matrix.
    Processes `chunk_size` Kalshi rows at a time against all Polymarket embeddings.
    Peak memory: chunk_size × M × 4 bytes (e.g. 500 × 51K × 4 = ~100MB).
    """
    import numpy as np  # type: ignore

    matches: list[dict[str, Any]] = []
    n_kalshi = len(kalshi_valid)

    for start in range(0, n_kalshi, chunk_size):
        end = min(start + chunk_size, n_kalshi)
        chunk = k_embeddings[start:end]  # (chunk_size, dim)
        sim_chunk = np.dot(chunk, p_embeddings.T)  # (chunk_size, M)

        ki_offsets, pi_vals = np.where(sim_chunk >= threshold)
        for ki_off, pi in zip(ki_offsets.tolist(), pi_vals.tolist()):
            ki = start + ki_off
            _, km, k_title = kalshi_valid[ki]
            _, pm, p_title = poly_valid[pi]
            score = float(sim_chunk[ki_off, pi])
            matches.append({
                "kalshi_ticker": str(km.get("ticker") or ""),
                "kalshi_title": k_title,
                "polymarket_slug": str(pm.get("slug") or ""),
                "polymarket_question": p_title,
                "similarity_score": round(score, 4),
                "category": _poly_category(pm) or str(km.get("category") or ""),
                "kalshi_expiry": _kalshi_expiry(km),
                "polymarket_end_date": _poly_end_date(pm),
            })

        if (start // chunk_size) % 20 == 0:
            logger.info("  similarity chunk %d/%d — %d matches so far", end, n_kalshi, len(matches))

    return matches


# ── Discovery ─────────────────────────────────────────────────────────────────

def run_discovery(
    threshold: float = 0.75,
    model_name: str = "all-MiniLM-L6-v2",
    output_dir: str = "data",
    max_kalshi: int = 10_000,
    device: str = "cpu",
) -> list[dict[str, Any]]:
    t0 = time.time()
    logger.info("=== Embedding Market Discovery ===")

    # Fetch markets
    logger.info("Fetching Kalshi markets …")
    kalshi_raw = fetch_kalshi_markets()
    logger.info("Fetched %d Kalshi markets", len(kalshi_raw))

    logger.info("Fetching Polymarket markets …")
    poly_raw = fetch_polymarket_markets()
    logger.info("Fetched %d Polymarket markets", len(poly_raw))

    if not kalshi_raw or not poly_raw:
        logger.error("No markets fetched — aborting")
        return []

    # Build text lists, deduplicating Kalshi by event_ticker to collapse
    # price-bracket contracts (e.g. "S&P > 5000", "S&P > 5100") into one
    # representative market per event. Without this, 273K contracts → 36-min embed.
    seen_event_tickers: set[str] = set()
    seen_kalshi_titles: set[str] = set()
    kalshi_valid: list[tuple[int, dict, str]] = []
    for i, m in enumerate(kalshi_raw):
        title = _kalshi_title(m)
        if not title:
            continue
        event_ticker = str(m.get("event_ticker") or "").strip()
        dedup_key = event_ticker or title
        if dedup_key in seen_event_tickers:
            continue
        seen_event_tickers.add(dedup_key)
        kalshi_valid.append((i, m, title))

    poly_valid: list[tuple[int, dict, str]] = []
    for i, m in enumerate(poly_raw):
        title = _poly_title(m)
        if title:
            poly_valid.append((i, m, title))

    if max_kalshi and len(kalshi_valid) > max_kalshi:
        logger.info("Capping Kalshi to first %d events (was %d)", max_kalshi, len(kalshi_valid))
        kalshi_valid = kalshi_valid[:max_kalshi]

    logger.info(
        "After dedup: %d Kalshi events, %d Polymarket markets",
        len(kalshi_valid), len(poly_valid),
    )

    k_texts = [t for _, _, t in kalshi_valid]
    p_texts = [t for _, _, t in poly_valid]

    # Generate embeddings
    k_embeddings = build_embeddings(k_texts, model_name, device)
    p_embeddings = build_embeddings(p_texts, model_name, device)

    # Chunked similarity — avoids materialising the full N×M matrix (~55GB for 271K×51K)
    logger.info(
        "Computing %d × %d cosine similarities in chunks of 500 …",
        len(k_texts), len(p_texts),
    )
    matches = find_matches_chunked(
        k_embeddings, p_embeddings, kalshi_valid, poly_valid, threshold, chunk_size=500
    )

    # Sort by score descending
    matches.sort(key=lambda x: x["similarity_score"], reverse=True)

    elapsed = time.time() - t0
    logger.info(
        "Found %d matches above %.2f in %.1fs (from %d × %d pairs)",
        len(matches), threshold, elapsed, len(k_texts), len(p_texts),
    )

    # Write outputs
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    json_path = out / "discovered_mappings.json"
    with json_path.open("w") as f:
        json.dump(matches, f, indent=2)
    logger.info("Wrote %s", json_path)

    csv_path = out / "discovered_mappings.csv"
    fields = [
        "kalshi_ticker", "kalshi_title", "polymarket_slug", "polymarket_question",
        "similarity_score", "category", "kalshi_expiry", "polymarket_end_date",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(matches)
    logger.info("Wrote %s", csv_path)

    return matches


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Embedding-based cross-platform market discovery")
    parser.add_argument("--threshold", type=float, default=0.75, help="Cosine similarity threshold (default: 0.75)")
    parser.add_argument("--model", default="all-MiniLM-L6-v2", help="Sentence-transformers model name")
    parser.add_argument("--output", default="data", help="Output directory for JSON/CSV files")
    parser.add_argument("--max-kalshi", type=int, default=10_000, help="Max Kalshi events after dedup (default: 10000)")
    parser.add_argument("--device", default="cpu", help="Torch device: cpu, mps, cuda (default: cpu)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    matches = run_discovery(
        threshold=args.threshold,
        model_name=args.model,
        output_dir=args.output,
        max_kalshi=args.max_kalshi,
        device=args.device,
    )

    print(f"\n{'='*60}")
    print(f"DISCOVERY COMPLETE: {len(matches)} matches found")
    print(f"{'='*60}")
    if matches:
        print("\nTop 20 matches:")
        for m in matches[:20]:
            print(f"  [{m['similarity_score']:.3f}] {m['kalshi_ticker']} <-> {m['polymarket_slug']}")
            print(f"    Kalshi:      {m['kalshi_title'][:80]}")
            print(f"    Polymarket:  {m['polymarket_question'][:80]}")
            print()


if __name__ == "__main__":
    main()
