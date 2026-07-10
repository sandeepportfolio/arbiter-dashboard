"""Cross-venue mapping coherence checks.

Added 2026-07-10 after the Senate party-swap incident: DEM_SENATE_2026 /
GOP_SENATE_2026 carried ForecastEx conids for the OPPOSITE party
(verified via IBKR /iserver/contract/info local_symbol), so 115 executed
"arbs" were two legs of the same directional bet. Semantic/title validation
cannot catch this (both contracts are literally "US Senate Majority"); these
two detectors can:

- ``max_yes_divergence``: two venues quoting the SAME outcome must agree
  within a tolerance. A wrong-contract mapping shows a large, PERSISTENT
  divergence (swapped DEM: kalshi yes 0.44 vs FX "yes" 0.61).
- ``party_conflict``: a canonical whose name/aliases carry a party token
  must not map to an FX contract whose local_symbol names the other party.

Both are consumed by the auto-validator's promotion gate and confirmed-
mapping sweep. Genuine fat arbs (divergence just above tolerance) get
quarantined to operator review too — one manual confirmation before a
1-lot edge can compound into a 100-lot book is the intended cost.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

# Max tolerated |implied_yes_A - implied_yes_B| across venues. Real
# cross-venue arb divergences are a few cents and get consumed; the
# swapped-party mappings sat at 0.09-0.17 for days. Env-overridable.
try:
    DEFAULT_MAX_YES_DIVERGENCE = float(
        os.getenv("MAPPING_COHERENCE_MAX_DIVERGENCE", "0.08") or "0.08"
    )
except (TypeError, ValueError):
    DEFAULT_MAX_YES_DIVERGENCE = 0.08

_PARTY_TOKENS = {
    "democratic": {"dem", "dems", "democrat", "democrats", "democratic"},
    "republican": {"gop", "rep", "republican", "republicans"},
}


def max_yes_divergence(
    platform_yes: Dict[str, float],
) -> Tuple[float, Optional[Tuple[str, str]]]:
    """Largest pairwise |yes_A - yes_B| across venues with LIVE yes prices.

    Returns (0.0, None) when fewer than two venues have a positive yes
    price — a single venue is trivially coherent and dead quotes cannot
    vote.
    """
    live = [(p, float(y)) for p, y in platform_yes.items() if y and float(y) > 0]
    if len(live) < 2:
        return 0.0, None
    worst = 0.0
    worst_pair: Optional[Tuple[str, str]] = None
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            div = abs(live[i][1] - live[j][1])
            if div > worst:
                worst = div
                worst_pair = (live[i][0], live[j][0])
    return worst, worst_pair


def canonical_party(canonical_text: str) -> Optional[str]:
    """Party implied by a canonical id/aliases blob, or None."""
    tokens = set(
        canonical_text.lower().replace("_", " ").replace("-", " ").split()
    )
    hits = {
        party for party, keywords in _PARTY_TOKENS.items() if tokens & keywords
    }
    if len(hits) == 1:
        return next(iter(hits))
    # Ambiguous (both parties named) or none: no verdict.
    return None


def party_conflict(canonical_text: str, fx_local_symbol: str) -> Optional[str]:
    """Conflict description when the canonical's party contradicts the FX
    contract symbol's party; None when clean or undeterminable.

    Missing/empty symbol returns None — the PROMOTION gate is responsible
    for failing closed on missing symbols when the canonical carries party
    tokens.
    """
    want = canonical_party(canonical_text)
    if want is None or not fx_local_symbol:
        return None
    have = canonical_party(fx_local_symbol)
    if have is None or have == want:
        return None
    return (
        f"canonical implies {want} but ForecastEx contract "
        f"{fx_local_symbol!r} is {have}"
    )
