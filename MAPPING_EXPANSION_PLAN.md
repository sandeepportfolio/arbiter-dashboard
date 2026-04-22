# Arbiter Market Mapping Expansion Plan

**Date:** 2026-04-22
**Author:** Claude (research agent)
**Status:** Research complete, ready for implementation

---

## 1. Current State Audit

### What exists today

The mapping system has four confirmed pairs, all 2026 midterm politics markets (DEM/GOP × House/Senate). The discovery pipeline in `auto_discovery.py` already does full-catalog pulls from both platforms, scores candidates via token-overlap heuristics, and feeds them through an 8-gate auto-promote pipeline that includes an LLM verifier (Claude Haiku). The infrastructure is solid but underutilized because the seed list is narrow and the similarity threshold is conservative.

### Platform market counts (live API data, April 22 2026)

| Platform | Open events | Open markets (est.) | Non-sports events |
|----------|------------|--------------------|--------------------|
| **Kalshi** | 5,921 | ~3,500+ | 2,975 |
| **Polymarket (Gamma)** | 20,100+ | 30,100+ | ~8,000+ (est.) |

### Kalshi category breakdown (5,921 open events)

| Category | Events | Notes |
|----------|--------|-------|
| Sports | 2,946 | MLB, NBA, NFL, golf, tennis, MMA |
| Elections | 1,273 | US + international, Pope, party primaries |
| Entertainment | 541 | Film, TV, music, celebrity |
| Politics | 340 | Geopolitics, policy, government actions |
| Economics | 313 | Unemployment, CPI, GDP, IPOs, billionaires |
| Crypto | 86 | BTC/ETH/SOL price, protocol events |
| Companies | 85 | AGI timelines, CEO changes, IPO races |
| Mentions | 73 | Earnings call keyword bets |
| Climate and Weather | 72 | Temperature, hurricanes, earthquakes |
| Science and Technology | 65 | Mars, fusion, moon landings |
| Financials | 55 | Oil prices, IPO races, S&P targets |
| Social | 24 | Population, cultural trends |
| Commodities | 24 | Oil, gold, agricultural |
| World | 16 | Geopolitical events |
| Health | 7 | FDA approvals, disease milestones |
| Transportation | 1 | Rail/infrastructure |

### Polymarket tag breakdown (20,100+ open events, top tags)

| Tag | Events | Notes |
|-----|--------|-------|
| Sports | 9,328 | NBA, MLB, NHL, NFL, soccer, esports |
| Games | 7,798 | Overlaps with sports (O/U, spreads) |
| Crypto | 2,942 | BTC, ETH, SOL prices, protocol events |
| Politics | 2,849 | US elections, Trump, international |
| Crypto Prices | 2,519 | Price-specific crypto markets |
| Culture | 1,001 | Entertainment, celebrity, media |
| Geopolitics | 703 | Wars, treaties, international relations |
| Weather | 382 | Temperature, storms, climate |
| Business | 370 | IPOs, M&A, company events |
| USA Election | 472 | Midterms, special elections |

---

## 2. Overlap Analysis: Where Are the Hundreds of Pairs?

### Category-by-category overlap estimate

| Category | Kalshi events | Polymarket events (est.) | Estimated overlapping pairs | Confidence |
|----------|--------------|------------------------|---------------------------|------------|
| **Politics/Elections** | 1,613 | 2,849 | **150-300** | High |
| **Economics** | 313 | ~200 | **40-80** | High |
| **Crypto prices** | 86 | 2,519 | **30-60** | High |
| **Sports** | 2,946 | 9,328 | **200-500+** | Medium |
| **Entertainment/Culture** | 541 | 1,001 | **30-80** | Medium |
| **Weather/Climate** | 72 | 382 | **15-30** | Medium |
| **Science/Tech** | 65 | ~100 | **10-20** | Low |
| **Companies/Business** | 85 | 370 | **10-30** | Medium |
| **Financials** | 55 | ~100 | **10-20** | Medium |
| **TOTAL** | | | **495-1,120** | |

### Why the overlap is large but hard to find

The platforms use completely different naming conventions, market structures, and identifiers. Examples of the same market looking very different:

- Kalshi: `CONTROLH-2026-D` "Democrats win House 2026 midterms"
- Polymarket: `paccc-usho-midterms-2026-11-03-dem` "Will the Democratic Party win the House in the 2026 Midterms?"

- Kalshi: `KXBTC-26DEC3100-T109999.99` (price bucket contract)
- Polymarket: `btc-above-110k-april-30` "Will Bitcoin be above $110,000 on April 30?"

- Kalshi: `KXFED-26JUN18` "Fed Funds rate after June 2026 meeting"
- Polymarket: `fed-rate-cut-june-2026` "Will the Fed cut rates at the June 2026 FOMC meeting?"

The token-overlap heuristic in `_candidate_score()` can catch many of these, but semantic matching would catch far more.

### High-value overlap categories (ranked by arbitrage potential)

1. **Politics/Elections** — Highest overlap, good liquidity on both platforms, binary outcomes, long-dated so prices diverge more. The 2026 midterms alone have hundreds of individual race markets on both platforms.

2. **Economics (Fed, CPI, unemployment)** — Both platforms list FOMC meeting outcomes, CPI prints, unemployment readings. These are high-volume, time-specific, and resolve to the same government data source. Likely 40-80 tradable pairs.

3. **Crypto prices** — Both platforms have BTC/ETH/SOL price targets. The challenge is matching exact strike prices and dates. Kalshi uses bucket-style contracts (e.g., "BTC above $100k by Dec 31") while Polymarket uses similar binary framing.

4. **Sports** — Massive volume on both platforms but resolution-date matching is critical (same game, same date). The auto-discovery pipeline's sports date-matching logic already handles this, but the sheer number (2,946 × 9,328 potential comparisons) needs efficient filtering.

5. **Entertainment/Culture** — Award shows (Oscars, Grammys), box office predictions, TV renewals. Lower liquidity but often wide spreads.

---

## 3. Discovery Strategy: From 4 to 400+ Pairs

### 3A. Immediate wins (no code changes)

Lower the `min_score` parameter in `discover()` from 0.25 to 0.15 — the current threshold is reasonable but misses semantic matches where platform naming differs significantly. The token-overlap index already pre-filters candidates, so a lower threshold just means more candidates reach the LLM verifier, which is the real quality gate.

### 3B. Embedding-based semantic matching

**Recommendation: Yes, add embeddings. This is the single highest-impact change.**

The current matching uses Jaccard token overlap plus a custom `similarity_score()` function. This misses semantic equivalents like "Fed cuts rates" vs "FOMC rate decision" or "Democrats win" vs "Democratic Party victory."

**Implementation approach:**

```
sentence-transformers (all-MiniLM-L6-v2)
  → 384-dim embeddings per market title
  → cosine similarity matrix
  → threshold at 0.70 for candidates
  → ~50ms per 1000 markets on CPU
```

The `all-MiniLM-L6-v2` model is 80MB, runs on CPU, and produces 384-dimensional embeddings. For 3,500 Kalshi markets × 30,000 Polymarket markets, a full cross-similarity matrix is impractical (105M comparisons), but with category-based pre-filtering and the existing token index, we only need to compute embeddings for the ~10,000 candidate pairs that share at least one meaningful token.

**Cost/performance estimate:**
- Embedding generation: ~2 seconds for 30,000 titles on CPU
- Cosine similarity for 10,000 pre-filtered pairs: <100ms
- Total discovery pass: <30 seconds (vs. current ~10-15 seconds)
- Memory: ~50MB for embeddings + model weights
- No API costs — runs locally

**Alternative: Use Claude Haiku embeddings via API.** Anthropic doesn't currently offer an embeddings endpoint, but we could use the existing LLM verifier in batch mode (see 3C below) as a semantic filter. However, local sentence-transformers is faster and cheaper for the initial candidate-generation stage.

### 3C. Batch LLM verification

The current `llm_verifier.py` makes one API call per candidate pair. For 500+ candidates, this is:
- 500 API calls × ~0.5s each = ~250 seconds sequentially
- At $0.25/M input tokens, ~500 calls × ~200 tokens = $0.025 total (very cheap)

**Optimization: Batch 10 pairs per LLM call.**

```python
# Instead of 500 individual calls, send 50 batched calls
# Each call verifies 10 pairs simultaneously
# System prompt: "For each pair below, answer YES/NO/MAYBE..."
# Reduces wall-clock time from 250s to ~25s
```

The existing `auto_promote.py` already uses `asyncio.Semaphore(concurrency=8)` for parallel verification. Combining batching with concurrency:
- 50 batched calls ÷ 8 concurrent = ~7 rounds × 0.5s = ~3.5 seconds
- 10x faster than current approach

### 3D. Category-based pre-filtering

Add a category mapping layer that converts platform-specific categories into a shared taxonomy before scoring:

```python
CATEGORY_MAP = {
    # Kalshi → canonical
    "Elections": "politics",
    "Politics": "politics",
    "Climate and Weather": "weather",
    "Science and Technology": "tech",
    "Financials": "finance",
    "Companies": "business",
    "Mentions": "business",  # earnings call mentions
    "Commodities": "finance",
    
    # Polymarket tags → canonical
    "USA Election": "politics",
    "Trump": "politics",
    "Trump Presidency": "politics",
    "Crypto Prices": "crypto",
    "Geopolitics": "politics",
    "Business": "business",
    "EPL": "sports",
    "NBA": "sports",
    # ... etc
}
```

This already partially exists in `_normalize_category()` and `_CATEGORY_ALIASES` in `auto_discovery.py`, but it needs expansion to cover the full Polymarket tag vocabulary.

### 3E. Event-level matching (group markets, not individual contracts)

Both platforms organize individual contracts under parent events. Instead of matching 3,500 × 30,000 individual markets, match at the event level first:

1. Match Kalshi events (5,921) against Polymarket events (20,100+) using embeddings
2. For matched event pairs, enumerate child markets on both sides
3. Match child markets within paired events (much smaller search space)

This reduces the search space dramatically and improves accuracy because child markets within a matched event are almost certainly about the same underlying question.

---

## 4. Liquidity Analysis

### Which overlapping markets have tradable liquidity?

Based on the API data, the Kalshi API exposes `yes_bid`, `yes_ask`, `volume`, and orderbook data per market. Polymarket's Gamma API returns `liquidity`, `volume`, `volume24hr`, `bestBid`, `bestAsk`, and `spread` per market.

**Liquidity tiers for arbitrage viability:**

| Tier | Both-side depth | Estimated overlapping markets | Arbitrage viable? |
|------|----------------|------------------------------|-------------------|
| A: >$1,000 depth | Both orderbooks >$1K | ~50-100 | Yes, $50-100 per trade |
| B: $100-$1,000 | Moderate liquidity | ~100-200 | Yes, $10-50 per trade |
| C: $10-$100 | Thin orderbooks | ~200-400 | Marginal, fees eat the edge |
| D: <$10 | Negligible | ~200+ | Not tradable |

**High-liquidity categories (most likely to be in Tier A/B):**
1. **2026 midterm races** — High volume on both platforms (especially House/Senate control, key swing states)
2. **Fed rate decisions** — Both platforms see heavy volume around FOMC meetings
3. **BTC/ETH price milestones** — Crypto markets are the highest-volume category on Polymarket
4. **Trump-related markets** — 1,211 Polymarket events tagged "Trump," heavy Kalshi coverage
5. **Major sports finals** — NBA/NFL championship markets draw large volume on both

**Implementation: Track liquidity in the mapping record.**

Add `kalshi_volume_24h`, `poly_volume_24h`, `kalshi_depth_usd`, `poly_depth_usd` fields to the candidate payload. Sort the auto-discovery output by minimum-side liquidity so the operator dashboard surfaces the most tradable pairs first.

---

## 5. Three-Platform Arbitrage (Coinbase Predict)

### Architecture change

Adding a third platform (Coinbase Predict or any other) changes the matching problem from pairwise to multi-way.

**Current:** O(K × P) where K = Kalshi markets, P = Polymarket markets
**Three platforms:** Naively O(K × P × C) — but this is avoidable.

### Better approach: Hub-and-spoke canonical mapping

Instead of comparing all platforms against each other, map every platform's markets to a canonical question ID:

```
Kalshi market  →  canonical_id  ←  Polymarket market
                       ↑
              Coinbase market
```

**Algorithm:**
1. For each platform, generate embeddings for all market titles
2. Cluster all embeddings across all platforms using approximate nearest neighbors (FAISS or Annoy)
3. Each cluster represents a unique real-world question
4. Markets from different platforms in the same cluster are potential arb legs

**Complexity:** O(N log N) where N = total markets across all platforms (using ANN index), regardless of the number of platforms. Adding a fourth or fifth platform doesn't increase algorithmic complexity.

**Data model change:**

```sql
-- Replace pairwise kalshi_market_id / polymarket_slug with:
CREATE TABLE platform_legs (
    canonical_id    VARCHAR(60) REFERENCES market_mappings(canonical_id),
    platform        VARCHAR(30) NOT NULL,  -- 'kalshi', 'polymarket_us', 'coinbase'
    market_id       VARCHAR(200) NOT NULL,
    market_title    TEXT,
    volume_24h      DECIMAL,
    depth_usd       DECIMAL,
    last_price       DECIMAL,
    updated_at      TIMESTAMPTZ,
    PRIMARY KEY (canonical_id, platform)
);
```

This makes the system N-platform from day one. The current `kalshi_market_id` / `polymarket_slug` columns become a special case of this table.

### Three-way arbitrage math

With three platforms, arbitrage opportunities become richer:

- **Binary YES/NO across 3 venues:** If Kalshi YES + Polymarket NO + Coinbase NO < $1.00, there's a riskless profit by buying the cheapest YES and cheapest NO across any two platforms. The third platform is optionally used if its price improves the spread.

- **Multi-leg:** With 3 platforms you can also find triangular price discrepancies: buy YES on A, sell YES on B (if B's YES > A's YES), creating a locked profit without needing to take the NO side.

---

## 6. Concrete Code Changes

### Phase 1: Aggressive discovery (1-2 days)

**File: `arbiter/mapping/auto_discovery.py`**

1. **Add scheduled discovery loop.** Currently discovery is called manually. Add a background task:

```python
async def discovery_loop(
    kalshi_client, polymarket_us_client, mapping_store,
    interval_seconds: int = 900,  # every 15 minutes
    promotion_settings: dict | None = None,
):
    """Run discovery on startup and every 15 minutes."""
    while True:
        try:
            count = await discover(
                kalshi_client, polymarket_us_client, mapping_store,
                min_score=0.15,  # lowered from 0.25
                max_candidates=1000,  # raised from 500
                promotion_settings=promotion_settings,
            )
            logger.info("discovery_loop: discovered %d candidates", count)
        except Exception:
            logger.exception("discovery_loop: error in discovery pass")
        await asyncio.sleep(interval_seconds)
```

Wire this into `arbiter/main.py` as an `asyncio.create_task()` at startup.

2. **Expand category aliases** in `_CATEGORY_ALIASES` and `_AUTO_CATEGORY_LABELS`:

```python
_AUTO_CATEGORY_LABELS = {
    "politics", "sports", "economics", "finance", "crypto",
    "geopolitics", "tech", "weather", "culture", "business",
    "entertainment", "health", "commodities", "science",
}

_CATEGORY_ALIASES = {
    "elections": "politics",
    "election": "politics",
    "usa election": "politics",
    "trump": "politics",
    "trump presidency": "politics",
    "world": "geopolitics",
    "international": "geopolitics",
    "middle east": "geopolitics",
    "sport": "sports",
    "nba": "sports", "nfl": "sports", "mlb": "sports",
    "nhl": "sports", "soccer": "sports", "epl": "sports",
    "climate and weather": "weather",
    "science and technology": "tech",
    "financials": "finance",
    "crypto prices": "crypto",
    "companies": "business",
    "mentions": "business",
    "commodities": "finance",
}
```

3. **Add liquidity tracking** to `_candidate_payload()`:

```python
def _candidate_payload(...) -> dict[str, Any]:
    # ... existing code ...
    payload["kalshi_volume"] = float(km.get("volume") or km.get("volume_24h") or 0)
    payload["poly_volume"] = float(pm.get("volume") or pm.get("volume24hr") or 0)
    payload["kalshi_liquidity"] = float(km.get("liquidity") or 0)
    payload["poly_liquidity"] = float(pm.get("liquidity") or 0)
    payload["min_side_volume"] = min(payload["kalshi_volume"], payload["poly_volume"])
    return payload
```

### Phase 2: Embedding-based matching (2-3 days)

**New file: `arbiter/mapping/embedding_matcher.py`**

```python
"""
Semantic embedding matcher for cross-platform market discovery.
Uses sentence-transformers (all-MiniLM-L6-v2) for local inference.
"""
import logging
import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("arbiter.mapping.embedding_matcher")

_MODEL_NAME = "all-MiniLM-L6-v2"
_model: SentenceTransformer | None = None

def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info("Loaded embedding model: %s", _MODEL_NAME)
    return _model

def embed_titles(titles: list[str]) -> np.ndarray:
    """Encode market titles into 384-dim embeddings."""
    model = _get_model()
    return model.encode(titles, normalize_embeddings=True, show_progress_bar=False)

def find_semantic_matches(
    kalshi_titles: list[str],
    poly_titles: list[str],
    threshold: float = 0.70,
    max_matches_per_market: int = 5,
) -> list[tuple[int, int, float]]:
    """
    Find semantically similar market pairs across platforms.
    Returns list of (kalshi_idx, poly_idx, cosine_similarity).
    """
    k_emb = embed_titles(kalshi_titles)
    p_emb = embed_titles(poly_titles)
    
    # Cosine similarity matrix (embeddings are already normalized)
    sim_matrix = k_emb @ p_emb.T
    
    matches = []
    for k_idx in range(len(kalshi_titles)):
        top_indices = np.argsort(sim_matrix[k_idx])[::-1][:max_matches_per_market]
        for p_idx in top_indices:
            score = float(sim_matrix[k_idx, p_idx])
            if score >= threshold:
                matches.append((k_idx, int(p_idx), score))
    
    return sorted(matches, key=lambda x: -x[2])
```

**Integration into `auto_discovery.py`:**

After the token-overlap pass, run the embedding matcher on all markets that didn't already match. This catches semantic equivalents the token heuristic misses. Merge results, deduplicate, and proceed to the existing scoring/promotion pipeline.

**Dependency:** Add `sentence-transformers` and `numpy` to `requirements.txt`. The model downloads once (~80MB) and caches locally.

### Phase 3: Batch LLM verification (1 day)

**File: `arbiter/mapping/llm_verifier.py`**

Add a `verify_batch()` function alongside the existing `verify()`:

```python
async def verify_batch(
    pairs: list[tuple[str, str]],
    batch_size: int = 10,
) -> list[Literal["YES", "NO", "MAYBE"]]:
    """Verify multiple pairs in a single LLM call for efficiency."""
    results = []
    for i in range(0, len(pairs), batch_size):
        batch = pairs[i:i + batch_size]
        # Check cache first
        uncached = []
        for k_q, p_q in batch:
            key = frozenset({k_q, p_q})
            if key in _cache:
                results.append(_cache[key])
            else:
                uncached.append((k_q, p_q))
        
        if not uncached:
            continue
        
        # Build batch prompt
        lines = []
        for j, (k_q, p_q) in enumerate(uncached, 1):
            lines.append(f"Pair {j}:")
            lines.append(f"  Q1 (Kalshi): {k_q}")
            lines.append(f"  Q2 (Polymarket): {p_q}")
        
        prompt = "\n".join(lines) + "\n\nFor each pair, answer YES, NO, or MAYBE."
        
        # Single API call for the batch
        resp = await client.messages.create(
            model=_MODEL, max_tokens=256,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        # Parse batch response...
        batch_results = _parse_batch_answer(resp.content[0].text, len(uncached))
        results.extend(batch_results)
    
    return results
```

### Phase 4: Multi-platform support (3-5 days)

**New file: `arbiter/mapping/platform_registry.py`**

```python
"""
Platform-agnostic market registry.
Replaces hardcoded kalshi/polymarket columns with a flexible leg-based model.
"""

@dataclass
class PlatformLeg:
    platform: str           # "kalshi", "polymarket_us", "coinbase"
    market_id: str
    title: str
    volume_24h: float = 0.0
    depth_usd: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    fee_rate: float = 0.0

@dataclass  
class CanonicalMarket:
    canonical_id: str
    description: str
    category: str
    resolution_date: str | None
    legs: dict[str, PlatformLeg]  # platform → leg
    
    @property
    def platforms(self) -> list[str]:
        return list(self.legs.keys())
    
    @property
    def is_arbitrageable(self) -> bool:
        return len(self.legs) >= 2
    
    def best_arb_pair(self) -> tuple[PlatformLeg, PlatformLeg] | None:
        """Find the two legs with the widest price discrepancy."""
        if len(self.legs) < 2:
            return None
        legs = list(self.legs.values())
        # Find min YES price and max NO price across platforms
        # ... arbitrage math here
```

**SQL migration:**

```sql
CREATE TABLE platform_legs (
    canonical_id    VARCHAR(60) NOT NULL,
    platform        VARCHAR(30) NOT NULL,
    market_id       VARCHAR(200) NOT NULL,
    market_title    TEXT,
    volume_24h      DECIMAL DEFAULT 0,
    depth_usd       DECIMAL DEFAULT 0,
    best_bid        DECIMAL DEFAULT 0,
    best_ask        DECIMAL DEFAULT 0,
    fee_rate        DECIMAL DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (canonical_id, platform),
    FOREIGN KEY (canonical_id) REFERENCES market_mappings(canonical_id)
);

CREATE INDEX idx_legs_platform ON platform_legs(platform);
CREATE INDEX idx_legs_market ON platform_legs(platform, market_id);
```

Backfill from existing `kalshi_market_id` and `polymarket_slug` columns on `market_mappings`.

---

## 7. Implementation Roadmap

| Phase | Work | Time | Expected pairs |
|-------|------|------|----------------|
| 1a | Lower thresholds, expand categories, add discovery loop | 1 day | 30-50 candidates |
| 1b | Add liquidity tracking to candidates | 0.5 days | Same count, ranked by tradability |
| 2 | Embedding-based matching | 2-3 days | 100-200 candidates |
| 3 | Batch LLM verification | 1 day | 50-100 confirmed (from candidate pool) |
| 4 | Multi-platform data model | 3-5 days | Future-proofs for 3+ platforms |
| 5 | Coinbase Predict integration | 2-3 days | +20-50 additional legs |

**Realistic target after Phases 1-3:** 100-200 confirmed tradable pairs within 2 weeks, with 50-100 having sufficient liquidity for the $1K-per-platform capital constraint.

---

## 8. Risk Considerations

**False positives are expensive.** A bad mapping means buying YES on one platform and NO on a different question — not an arbitrage, just two independent bets. The 8-gate auto-promote pipeline exists for this reason, and it should stay strict. The expansion should increase *candidates* aggressively but keep *confirmation* conservative.

**Rate limits matter.** Kalshi's API has no documented rate limit but the existing `budget_rps=2.0` is conservative. Polymarket US limits to 60 req/min across all endpoints. The 15-minute discovery loop with bulk fetches stays well within both limits.

**Embedding model drift.** The `all-MiniLM-L6-v2` model is general-purpose. For prediction market text specifically, fine-tuning on a dataset of known-equivalent market pairs would improve accuracy. Start with the off-the-shelf model and collect match/no-match labels from operator reviews to create training data.

**Resolution divergence is the real danger.** Two markets can ask "the same question" but resolve differently due to different settlement rules, different reference dates, or different data sources. The structured `resolution_check.py` Layer 1 gate plus the LLM Layer 2 gate handle this, but expanding the `_SOURCE_EQUIV_GROUPS` list in `resolution_check.py` will be important as we move beyond politics into economics (BLS vs. BEA), crypto (different oracle sources), and sports (different stat providers).

---

## Sources

- [Kalshi API Documentation](https://docs.kalshi.com/api-reference/market/get-markets)
- [Polymarket Gamma API Documentation](https://docs.polymarket.com/developers/gamma-markets-api/get-markets)
- [Polymarket US API Guide](https://agentbets.ai/guides/polymarket-us-api-guide/)
- [Polymarket Documentation](https://docs.polymarket.com/api-reference/markets/list-markets)
- [Kalshi vs Polymarket Comparison](https://www.wsn.com/prediction-markets/kalshi-vs-polymarket/)
- [Sentence Transformers Documentation](https://sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html)
- [Prediction Market Arbitrage Explained](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works)
- [Kalshi Market Data](https://kalshi.com/market-data)
- [Polymarket Predictions](https://polymarket.com/predictions/all)
