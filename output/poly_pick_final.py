"""Pick final happy + fok constants. Re-verify book, fetch categories/tags."""

import json
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request

OUT = r"C:\Users\sande\Documents\arbiter-dashboard\output"


def http_get(url, timeout=15):
    req = Request(url, headers={"User-Agent": "arbiter-phase4-research/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# Map Polymarket tags/slugs to one of the approved categories
APPROVED = {"crypto", "sports", "politics", "geopolitics", "finance", "economics", "culture", "weather", "tech", "mentions"}

TAG_MAP = {
    # sports
    "sports": "sports", "mlb": "sports", "nba": "sports", "nfl": "sports", "nhl": "sports",
    "soccer": "sports", "football": "sports", "baseball": "sports", "basketball": "sports",
    "hockey": "sports", "world series": "sports", "stanley cup": "sports", "epl": "sports",
    "ufc": "sports", "mma": "sports", "boxing": "sports", "tennis": "sports", "golf": "sports",
    "mls": "sports",
    # politics
    "politics": "politics", "us-politics": "politics", "elections": "politics",
    "democratic nomination": "politics", "republican": "politics", "president": "politics",
    "senate": "politics", "house": "politics", "congress": "politics",
    # geopolitics
    "geopolitics": "geopolitics", "middle east": "geopolitics", "ukraine": "geopolitics",
    "russia": "geopolitics", "china": "geopolitics", "iran": "geopolitics", "israel": "geopolitics",
    "war": "geopolitics", "nato": "geopolitics",
    # finance
    "stocks": "finance", "earnings": "finance", "markets": "finance", "sp500": "finance",
    "nasdaq": "finance", "tesla": "finance", "nvidia": "finance", "palantir": "finance",
    # economics
    "economics": "economics", "fed": "economics", "interest rates": "economics",
    "rate hike": "economics", "rate cut": "economics", "inflation": "economics",
    "cpi": "economics", "gdp": "economics", "unemployment": "economics", "recession": "economics",
    # crypto
    "crypto": "crypto", "bitcoin": "crypto", "btc": "crypto", "ethereum": "crypto", "eth": "crypto",
    "solana": "crypto", "sol": "crypto",
    # culture
    "culture": "culture", "pop-culture": "culture", "awards": "culture", "oscars": "culture",
    "grammy": "culture", "emmy": "culture", "movies": "culture", "music": "culture",
    # tech
    "tech": "tech", "ai": "tech", "openai": "tech", "google": "tech", "apple": "tech",
    # weather
    "weather": "weather", "hurricane": "weather", "storm": "weather",
    # mentions
    "mentions": "mentions",
}


def infer_category(slug, question, tags):
    """Return one of the APPROVED categories."""
    text = f"{slug or ''} {question or ''}".lower()
    tag_list = []
    if tags:
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, dict):
                    tag_list.append(str(t.get("slug") or t.get("label") or "").lower())
                else:
                    tag_list.append(str(t).lower())
        elif isinstance(tags, str):
            tag_list = [tags.lower()]

    # Check tags first
    for t in tag_list:
        if t in TAG_MAP:
            return TAG_MAP[t]
        for key, cat in TAG_MAP.items():
            if key in t:
                return cat

    # Fall back to keyword search in text
    for key, cat in TAG_MAP.items():
        if key in text:
            return cat
    return "mentions"


def fetch_book(token_id):
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    return http_get(url)


def fetch_market_detail(slug):
    url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
    return http_get(url)


def best_ask(book):
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
    return best


def depth_at_or_below(book, target_price):
    asks = book.get("asks") or []
    total = 0.0
    for level in asks:
        try:
            p = float(level.get("price"))
            s = float(level.get("size"))
        except Exception:
            continue
        if s > 0 and p <= target_price + 1e-9:
            total += s
    return total


def top_levels(book, side="asks", n=5):
    items = book.get(side) or []
    levels = []
    for level in items:
        try:
            p = float(level.get("price"))
            s = float(level.get("size"))
        except Exception:
            continue
        if s > 0:
            levels.append({"price": p, "size": s})
    reverse = side == "bids"
    levels.sort(key=lambda x: x["price"], reverse=reverse)
    return levels[:n]


def pick_happy(winners):
    """Prefer: price<=0.15, depth>=50, not sports-draw (more stable), not near-expiry sports.
    Return a dict of final choice."""
    for r in winners:
        ba = r["best_ask_price"]
        depth = r.get("depth_at_best_ask") or 0
        slug = r["slug"]
        if ba is None or depth < 50:
            continue
        # Skip super-short-expiry sports event markets (e.g. today's draw)
        if "2026-04-1" in slug or "2026-04-2" in slug:
            # these expire within days — skip for safety
            continue
        # fetch detail for tags
        try:
            detail = fetch_market_detail(slug)
            time.sleep(0.2)
        except Exception:
            detail = []
        tags = None
        if isinstance(detail, list) and detail:
            tags = detail[0].get("tags") or detail[0].get("events") or []
        cat = infer_category(slug, r.get("question"), tags)
        # qty so that qty*price <= 5
        # minSize is the minimum; ensure qty >= minSize and qty*price<=5 and qty <= depth
        min_size = int(float(r.get("orderMinSize") or 5))
        # pick qty = max(min_size, floor(5/price)) but capped
        import math
        max_by_cash = math.floor(5.0 / ba)
        qty = max(min_size, 1) if max_by_cash < min_size else max_by_cash
        # ensure qty*price <= 5 strictly — reduce if needed
        while qty * ba > 5.0:
            qty -= 1
        if qty < min_size:
            # cannot satisfy — skip
            continue
        return {
            "slug": slug,
            "question": r.get("question"),
            "token_id": r["token_id"],
            "best_ask": ba,
            "depth_at_best_ask": depth,
            "min_order_size": min_size,
            "category": cat,
            "tags_raw": tags,
            "qty": qty,
            "cost_usd": qty * ba,
            "detail_tags_debug": tags,
        }
    return None


def pick_fok(winners):
    """Want: depth_at_best_ask is small but > 0, and we can choose qty>depth with qty*price<=5."""
    for r in winners:
        ba = r["best_ask_price"]
        depth = r.get("depth_at_best_ask") or 0
        slug = r["slug"]
        if ba is None or depth <= 0:
            continue
        if ba > 0.20:
            continue  # keep cost low
        if depth >= 20:
            continue
        # we'll submit qty = ceil(depth) + 5 (or enough to bust book) subject to qty*price<=5
        import math
        qty = math.ceil(depth) + 5
        # cap by 5/price
        max_by_cash = math.floor(5.0 / ba)
        if qty > max_by_cash:
            qty = max_by_cash
        if qty <= depth:
            continue  # cannot bust the book within $5 — skip
        # need qty >= min_order_size
        min_size = int(float(r.get("orderMinSize") or 5))
        if qty < min_size:
            continue
        return {
            "slug": slug,
            "question": r.get("question"),
            "token_id": r["token_id"],
            "best_ask": ba,
            "depth_at_best_ask": depth,
            "min_order_size": min_size,
            "qty": qty,
            "cost_usd": qty * ba,
        }
    return None


def reverify(choice, label):
    """Re-fetch book right now to confirm ask + depth still match."""
    print(f"Re-verifying {label} token {choice['token_id'][:20]}... ", flush=True)
    try:
        book = fetch_book(choice["token_id"])
    except Exception as e:
        print(f"  error: {e}", flush=True)
        return None
    ba = best_ask(book)
    if ba is None:
        print(f"  no ask!", flush=True)
        return None
    depth = depth_at_or_below(book, ba[0])
    print(f"  ask now={ba[0]:.3f} (was {choice['best_ask']:.3f}), depth now={depth:.1f} (was {choice['depth_at_best_ask']:.1f})", flush=True)
    return {
        "best_ask_now": ba[0],
        "depth_now": depth,
        "top5_asks": top_levels(book, "asks", 5),
        "top5_bids": top_levels(book, "bids", 5),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    with open(f"{OUT}\\poly_happy_winners.json", "r", encoding="utf-8") as f:
        happy_winners = json.load(f)
    with open(f"{OUT}\\poly_fok_winners.json", "r", encoding="utf-8") as f:
        fok_winners = json.load(f)

    happy = pick_happy(happy_winners)
    fok = pick_fok(fok_winners)

    print("\n==== HAPPY PICK ====")
    print(json.dumps(happy, indent=2, default=str))
    print("\n==== FOK PICK ====")
    print(json.dumps(fok, indent=2, default=str))

    final = {"happy": happy, "fok": fok}

    if happy:
        final["happy"]["reverify"] = reverify(happy, "happy")
    if fok:
        final["fok"]["reverify"] = reverify(fok, "fok")

    with open(f"{OUT}\\poly_final_picks.json", "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, default=str)
    print("\nWrote poly_final_picks.json")


if __name__ == "__main__":
    main()
