"""Probe order books for top happy-path and thin-book candidates.

Happy-path criteria to satisfy:
- best ask <= 0.20
- depth (shares) at that ask price >= 5 contracts
- min_order_size * price <= 5  (we'll pick qty such that qty*price<=5, which is stronger)

FOK criteria:
- best ask price, depth at ask < intended qty (we'll pick qty > observed depth so FOK rejects)
- qty * price <= 5
"""

import json
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request

OUT = r"C:\Users\sande\Documents\arbiter-dashboard\output"


def http_get(url, timeout=15):
    req = Request(url, headers={"User-Agent": "arbiter-phase4-research/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_book(token_id):
    # Polymarket CLOB: /book?token_id=...
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    return http_get(url)


def best_ask(book):
    """Book 'asks' are sorted ascending-price in some endpoints, descending in others.
    Standard clob returns 'asks' array of {price, size} with LOWEST ask last (bid-ask ordering).
    We'll find min price ask with size>0."""
    asks = book.get("asks") or []
    best = None
    for level in asks:
        try:
            p = float(level.get("price"))
            s = float(level.get("size"))
        except Exception:
            continue
        if s <= 0:
            continue
        if best is None or p < best[0]:
            best = (p, s)
    return best  # (price, size) or None


def top_asks(book, n=5):
    asks = book.get("asks") or []
    levels = []
    for level in asks:
        try:
            p = float(level.get("price"))
            s = float(level.get("size"))
        except Exception:
            continue
        if s > 0:
            levels.append((p, s))
    levels.sort(key=lambda x: x[0])
    return levels[:n]


def depth_at_or_below(book, target_price):
    """Aggregate ask size at any price <= target_price (since we'd hit them)."""
    asks = book.get("asks") or []
    total = 0.0
    for level in asks:
        try:
            p = float(level.get("price"))
            s = float(level.get("size"))
        except Exception:
            continue
        if s <= 0:
            continue
        if p <= target_price + 1e-9:
            total += s
    return total


def probe(candidates, label, max_probe=80):
    results = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, m in enumerate(candidates[:max_probe]):
        tok = m.get("token_id") or (
            json.loads(m.get("clobTokenIds"))[0]
            if isinstance(m.get("clobTokenIds"), str)
            else (m.get("clobTokenIds") or [None])[0]
        )
        if not tok:
            continue
        try:
            book = fetch_book(tok)
        except Exception as e:
            print(f"[{label}] {i}: book error for {m.get('slug')}: {e}", flush=True)
            time.sleep(0.25)
            continue
        ba = best_ask(book)
        top5 = top_asks(book, 5)
        row = {
            "slug": m.get("slug"),
            "question": m.get("question"),
            "category": m.get("category"),
            "liquidity": m.get("liquidity"),
            "volume24hr": m.get("volume24hr"),
            "outcomes": m.get("outcomes"),
            "token_id": tok,
            "outcome_idx": m.get("outcome_idx"),
            "outcomePrices_meta": m.get("outcomePrices"),
            "best_ask_price": ba[0] if ba else None,
            "best_ask_size": ba[1] if ba else None,
            "top5_asks": top5,
            "orderMinSize": m.get("orderMinSize"),
            "observed_at": now_iso,
        }
        # for happy: want depth at or below best_ask >= 5
        if ba is not None:
            row["depth_at_best_ask"] = depth_at_or_below(book, ba[0])
        results.append(row)
        time.sleep(0.15)  # polite
    with open(f"{OUT}\\poly_{label}_probed.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    return results


def main():
    with open(f"{OUT}\\poly_happy_candidates.json", "r", encoding="utf-8") as f:
        happy = json.load(f)
    with open(f"{OUT}\\poly_fok_candidates.json", "r", encoding="utf-8") as f:
        fok = json.load(f)

    print(f"Probing {min(80, len(happy))} happy candidates...", flush=True)
    h_res = probe(happy, "happy", max_probe=80)

    print(f"Probing {min(80, len(fok))} thin candidates...", flush=True)
    f_res = probe(fok, "fok", max_probe=80)

    # Rank happy: ask<=0.20 AND depth_at_best_ask>=5
    happy_ok = [
        r for r in h_res
        if r["best_ask_price"] is not None
        and r["best_ask_price"] <= 0.20
        and (r.get("depth_at_best_ask") or 0) >= 5
        and (r.get("orderMinSize") is None or float(r["orderMinSize"]) * r["best_ask_price"] <= 5)
    ]
    happy_ok.sort(key=lambda r: (-(r.get("depth_at_best_ask") or 0), r["best_ask_price"]))
    print(f"\n==== HAPPY PATH WINNERS ({len(happy_ok)}) ====", flush=True)
    for r in happy_ok[:10]:
        print(
            f"  {r['slug'][:55]:55s}  ask=${r['best_ask_price']:.3f} depth={r['depth_at_best_ask']:.0f}  minSize={r.get('orderMinSize')}  cat={r.get('category')}",
            flush=True,
        )

    # Rank FOK: want thin — ask price reasonable, depth_at_best_ask small (e.g., <5), so qty > depth triggers FOK reject
    fok_ok = [
        r for r in f_res
        if r["best_ask_price"] is not None
        and 0.02 <= r["best_ask_price"] <= 0.50
        and r.get("depth_at_best_ask") is not None
        and r["depth_at_best_ask"] < 20  # thin
    ]
    # Prefer ones where best_ask <= 0.20 so qty*price stays <=5 with small qty
    fok_ok.sort(key=lambda r: (r["best_ask_price"], r["depth_at_best_ask"]))
    print(f"\n==== FOK (THIN) CANDIDATES ({len(fok_ok)}) ====", flush=True)
    for r in fok_ok[:10]:
        print(
            f"  {r['slug'][:55]:55s}  ask=${r['best_ask_price']:.3f} depth={r['depth_at_best_ask']:.1f}  cat={r.get('category')}",
            flush=True,
        )

    with open(f"{OUT}\\poly_happy_winners.json", "w", encoding="utf-8") as f:
        json.dump(happy_ok[:20], f, indent=2, default=str)
    with open(f"{OUT}\\poly_fok_winners.json", "w", encoding="utf-8") as f:
        json.dump(fok_ok[:20], f, indent=2, default=str)


if __name__ == "__main__":
    main()
