"""Pick a robust FOK candidate. Re-probe multiple times to confirm book is stable/thin."""
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
    return http_get(f"https://clob.polymarket.com/book?token_id={token_id}")


def best_ask(book):
    asks = book.get("asks") or []
    best = None
    for level in asks:
        try:
            p, s = float(level.get("price")), float(level.get("size"))
        except Exception:
            continue
        if s <= 0:
            continue
        if best is None or p < best[0]:
            best = (p, s)
    return best


def depth_at(book, target):
    total = 0.0
    for level in (book.get("asks") or []):
        try:
            p, s = float(level.get("price")), float(level.get("size"))
        except Exception:
            continue
        if s > 0 and p <= target + 1e-9:
            total += s
    return total


with open(f"{OUT}\\poly_fok_winners.json") as f:
    fok = json.load(f)

# Candidates with ask<=0.20 and depth<20, prefer depth<10
cands = [r for r in fok if r["best_ask_price"] and r["best_ask_price"] <= 0.20 and r.get("depth_at_best_ask", 0) < 20]
cands.sort(key=lambda r: r["depth_at_best_ask"])
print("Candidate pool (ask<=0.20, depth<20):")
for r in cands:
    print(f"  {r['slug'][:60]:60s} ask=${r['best_ask_price']:.3f} depth={r['depth_at_best_ask']:.1f}")

# Multi-sample each top candidate to pick the most stable thin book
results = []
for r in cands[:6]:
    token = r["token_id"]
    slug = r["slug"]
    samples = []
    for i in range(3):
        try:
            book = fetch_book(token)
        except Exception as e:
            print(f"  {slug}: sample {i}: error {e}")
            continue
        ba = best_ask(book)
        if ba is None:
            continue
        d = depth_at(book, ba[0])
        samples.append({"ask": ba[0], "size_at_best": ba[1], "depth_cum": d})
        time.sleep(1.0)
    results.append({"slug": slug, "token_id": token, "min_order_size": r.get("orderMinSize"), "samples": samples})

print("\n==== SAMPLES ====")
for r in results:
    print(f"\n{r['slug']}")
    for i, s in enumerate(r["samples"]):
        print(f"  sample{i}: ask=${s['ask']:.3f} size_at_best={s['size_at_best']:.1f} depth_cum={s['depth_cum']:.1f}")

# Pick: want smallest stable depth_cum across samples. Choose best candidate where max(depth) across samples keeps qty*price<=5.
import math

picked = None
for r in results:
    if not r["samples"]:
        continue
    max_depth = max(s["depth_cum"] for s in r["samples"])
    min_depth = min(s["depth_cum"] for s in r["samples"])
    asks = [s["ask"] for s in r["samples"]]
    max_ask = max(asks)
    # Need qty > max_depth (to reliably reject) AND qty*max_ask <= 5
    qty = math.ceil(max_depth) + 3  # generous buffer
    cost = qty * max_ask
    min_size = int(float(r.get("min_order_size") or 5))
    if cost > 5.0:
        continue
    if qty < min_size:
        continue
    picked = {
        "slug": r["slug"],
        "token_id": r["token_id"],
        "best_ask": max_ask,  # use worst-case max ask
        "depth_observed_max": max_depth,
        "depth_observed_min": min_depth,
        "min_order_size": min_size,
        "qty": qty,
        "cost_usd": cost,
        "samples": r["samples"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    break

print("\n==== PICKED FOK ====")
print(json.dumps(picked, indent=2, default=str))

if picked:
    with open(f"{OUT}\\poly_fok_picked.json", "w") as f:
        json.dump(picked, f, indent=2, default=str)
