"""
LLM Verifier — Layer 2 market-equivalence check via Claude Haiku.

Asks Claude whether two prediction-market questions resolve to the same
real-world outcome. Results are cached in-memory (LRU, up to 10k entries)
keyed by the frozenset of both questions so order does not matter.

Fail-safe: any API exception returns MAYBE. The auto-promote gate treats
MAYBE as "not-YES", so a flaky network can never accidentally promote.

Prompt caching: the system prompt uses cache_control={"type": "ephemeral"}
so it is cached at the Anthropic edge, reducing cost for repeated calls.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Literal, Optional

logger = logging.getLogger("arbiter.mapping.llm_verifier")

# ─── In-memory cache ──────────────────────────────────────────────────────────
# Keyed by frozenset({q1, q2}) so order doesn't matter.
# maxsize=10_000 keeps memory bounded.

_cache: dict[frozenset, Literal["YES", "NO", "MAYBE"]] = {}
_CACHE_MAX = 10_000

# ─── Model ────────────────────────────────────────────────────────────────────

_MODEL = "claude-haiku-4-5-20251001"

# ─── System prompt (cached at Anthropic edge) ─────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a prediction-market resolution expert. "
    "Your task is to determine whether two prediction-market questions "
    "resolve to the same real-world outcome. "
    "Answer with exactly one word: YES, NO, or MAYBE. "
    "Then optionally add a brief one-sentence reason.\n\n"
    "Rules:\n"
    "- YES: Both questions will be resolved by the exact same real-world event "
    "at the same time.\n"
    "- NO: The questions resolve to different events, different time windows, "
    "or have conflicting resolution criteria.\n"
    "- MAYBE: Insufficient information to determine equivalence, or the "
    "resolution criteria are ambiguous.\n\n"
    "Example:\n"
    "Q1: Will the Federal Reserve cut rates in May 2026?\n"
    "Q2: Will the Fed cut interest rates at the May 2026 FOMC meeting?\n"
    "Answer: YES - both resolve on the same FOMC meeting outcome."
)


# ─── Client factory ───────────────────────────────────────────────────────────

def _get_client():
    """Return a new AsyncAnthropic client. Separated for test mocking."""
    import anthropic
    return anthropic.AsyncAnthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )


# ─── Response parser ──────────────────────────────────────────────────────────

_ANSWER_RE = re.compile(r"\b(YES|NO|MAYBE)\b", re.IGNORECASE)


def _parse_answer(text: str) -> Literal["YES", "NO", "MAYBE"]:
    """Extract YES/NO/MAYBE from model response text. Returns MAYBE on ambiguity."""
    text = text.strip()
    matches = _ANSWER_RE.findall(text)
    if not matches:
        return "MAYBE"
    first = matches[0].upper()
    if first in ("YES", "NO", "MAYBE"):
        return first  # type: ignore[return-value]
    return "MAYBE"


# ─── Public API ───────────────────────────────────────────────────────────────

async def verify(
    kalshi_question: str,
    poly_question: str,
) -> Literal["YES", "NO", "MAYBE"]:
    """Layer 2 — ask Claude whether two markets resolve to the same outcome.

    Uses prompt caching on the system prompt. Cache key for in-memory cache:
    frozenset({kalshi_question, poly_question}).

    On API failure returns MAYBE (fail-safe — the auto-promote gate treats
    MAYBE as "not-YES", so a flaky network never accidentally promotes).
    """
    cache_key = frozenset({kalshi_question, poly_question})

    # Check in-memory cache first
    if cache_key in _cache:
        logger.debug("llm_verifier cache hit for pair")
        return _cache[cache_key]

    try:
        client = _get_client()
        resp = await client.messages.create(
            model=_MODEL,
            max_tokens=64,
            # System prompt with cache_control for prompt caching
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Q1 (Kalshi): {kalshi_question}\n"
                        f"Q2 (Polymarket): {poly_question}\n\n"
                        "Do these two markets resolve to the same real-world outcome? "
                        "Answer YES, NO, or MAYBE."
                    ),
                }
            ],
        )
        text = resp.content[0].text if resp.content else ""
        result = _parse_answer(text)

    except Exception as exc:
        # Fail-safe: any error → MAYBE so we never accidentally promote
        logger.warning(
            "llm_verifier API error (returning MAYBE): %s: %s",
            type(exc).__name__,
            exc,
        )
        result = "MAYBE"

    # Store in cache (evict oldest if over limit — simple FIFO)
    if len(_cache) >= _CACHE_MAX:
        # Remove an arbitrary old entry to stay bounded
        oldest_key = next(iter(_cache))
        del _cache[oldest_key]
    _cache[cache_key] = result

    return result
