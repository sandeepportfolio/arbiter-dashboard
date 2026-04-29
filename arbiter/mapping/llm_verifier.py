"""
LLM Verifier — Layer 2 market-equivalence check via Claude.

Asks Claude whether two prediction-market questions resolve to the same
real-world outcome. Results are cached in-memory (LRU, up to 10k entries)
keyed by the frozenset of both questions so order does not matter.

Supports two backends (auto-detected):
  1. Claude Code CLI (`claude --print`) — uses Max subscription, no API key needed.
     Set LLM_VERIFIER_BACKEND=cli or just have `claude` on PATH without ANTHROPIC_API_KEY.
  2. Anthropic API — set ANTHROPIC_API_KEY env var.

Fail-safe: any error returns MAYBE. The auto-promote gate treats
MAYBE as "not-YES", so a flaky network can never accidentally promote.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Literal

logger = logging.getLogger("arbiter.mapping.llm_verifier")

# ─── In-memory cache (mirrors persistent cache below) ─────────────────────────
_cache: dict[frozenset, Literal["YES", "NO", "MAYBE"]] = {}
_CACHE_MAX = 10_000

# ─── Persistent cache ─────────────────────────────────────────────────────────
# The container-local in-memory cache resets on every restart, so an LLM
# verdict that cost an Opus round-trip yesterday gets re-paid today. We
# back it with a small JSON file. The cache is keyed by a stable hash
# of (kalshi_q, poly_q) so order-flips collide.
#
# Default location lives inside the source tree (mapping/fixtures) so it
# is bind-mounted into the container with the rest of the mapping data.
# Override with LLM_VERIFIER_CACHE_PATH for tests / alternate deployments.
_CACHE_PATH = Path(
    os.environ.get(
        "LLM_VERIFIER_CACHE_PATH",
        str(Path(__file__).resolve().parent / "fixtures" / "llm_verifier_cache.json"),
    )
)
_CACHE_LOCK = threading.Lock()
_CACHE_DIRTY = False


def _cache_key(a: str, b: str) -> str:
    """Order-independent stable key for the persistent cache."""
    pair = sorted([(a or "").strip(), (b or "").strip()])
    return "|".join(pair)


def _load_persistent_cache() -> dict[str, str]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text() or "{}")
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("llm_verifier: cache unreadable at %s — %s", _CACHE_PATH, exc)
        return {}


def _persist_cache(entries: dict[str, str]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries))
        tmp.replace(_CACHE_PATH)
    except OSError as exc:
        logger.warning("llm_verifier: cache write failed at %s — %s", _CACHE_PATH, exc)


_persistent_cache: dict[str, str] = _load_persistent_cache()
logger.info("llm_verifier: loaded %d cached verdicts from %s", len(_persistent_cache), _CACHE_PATH)


def _cache_get(kalshi_q: str, poly_q: str) -> Literal["YES", "NO", "MAYBE"] | None:
    key = _cache_key(kalshi_q, poly_q)
    with _CACHE_LOCK:
        val = _persistent_cache.get(key)
    if val in ("YES", "NO", "MAYBE"):
        return val  # type: ignore[return-value]
    return None


def _cache_put(kalshi_q: str, poly_q: str, verdict: Literal["YES", "NO", "MAYBE"]) -> None:
    """Store ``verdict`` under both pairs' keys and flush to disk async-style.

    Persisting on every write would thrash the disk. Instead we mark the
    cache dirty and let a background flush every 50 writes pick it up.
    """
    global _CACHE_DIRTY
    key = _cache_key(kalshi_q, poly_q)
    with _CACHE_LOCK:
        prior = _persistent_cache.get(key)
        _persistent_cache[key] = verdict
        if prior != verdict:
            _CACHE_DIRTY = True
        # Flush every 25 dirty writes — bounds disk I/O without losing too much
        # work on container crashes.
        if _CACHE_DIRTY and len(_persistent_cache) % 25 == 0:
            try:
                _persist_cache(dict(_persistent_cache))
                _CACHE_DIRTY = False
            except Exception:
                pass


def flush_cache() -> None:
    """Public helper: force-persist the cache. Useful for clean shutdowns."""
    global _CACHE_DIRTY
    with _CACHE_LOCK:
        if _CACHE_DIRTY:
            _persist_cache(dict(_persistent_cache))
            _CACHE_DIRTY = False

# ─── Model ────────────────────────────────────────────────────────────────────
_API_MODEL = "claude-sonnet-4-6"  # Fallback API model for better accuracy
_CLI_MODEL = "claude-opus-4-7"  # CLI model — uses Max subscription's Opus 4.7 for highest accuracy

# ─── Prompt ───────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are a prediction-market resolution expert. "
    "Your task is to determine whether two prediction-market questions "
    "resolve to the same real-world outcome. "
    "Answer with exactly one word on the first line: YES, NO, or MAYBE. "
    "Then optionally add a brief one-sentence reason.\n\n"
    "Rules:\n"
    "- YES: Both questions will be resolved by the exact same real-world event "
    "at the same time. Minor phrasing differences (e.g. team abbreviations, "
    "platform-specific titling) do NOT block a YES if the underlying outcome "
    "is the same.\n"
    "- NO: The questions resolve to different events, different time windows, "
    "different scopes (e.g. game vs series), or have conflicting resolution criteria.\n"
    "- MAYBE: One question is missing critical detail to determine equivalence "
    "(e.g. ambiguous date), but the visible criteria are consistent.\n\n"
    "Common mappings to recognize:\n"
    "- 'Will Team A win on YYYY-MM-DD' ≡ '<sport>-A-B-YYYY-MM-DD' for the SAME team.\n"
    "- 'Will Party X win the Senate in 2026' ≡ 'usse-midterms-2026-11-03-x'.\n"
    "- 'BTC above $X by Y' from one venue ≡ 'will Bitcoin reach $X by Y' from the other.\n\n"
    "Examples:\n"
    "Q1: Will Houston win on 2026-04-29?\n"
    "Q2: aec-mlb-hou-bal-2026-04-29\n"
    "Answer: YES - both resolve on Houston winning the same MLB game.\n\n"
    "Q1: Will Lakers win their first-round series?\n"
    "Q2: Will Lakers win their game on 2026-04-29?\n"
    "Answer: NO - one is a series, the other is a single game.\n\n"
    "Q1: Will BTC be above $100K on 2026-12-31?\n"
    "Q2: Will Bitcoin be the best-performing asset of 2026?\n"
    "Answer: NO - different events (price threshold vs relative ranking)."
)


_CATEGORY_HINTS: dict[str, str] = {
    "sports": (
        "These are sports markets. Match by sport, league, date, and the "
        "specific team/player. Different abbreviations of the same team "
        "(BAR/FCB, ATX/AUS) ARE the same team."
    ),
    "politics": (
        "These are political markets. Match by office, party/candidate, "
        "year, and resolution body. Different phrasings of the same race "
        "ARE the same market (e.g. 'Senate Majority' = 'control of Senate')."
    ),
    "crypto": (
        "These are crypto markets. Match by asset, threshold price, and "
        "resolution date — small wording differences are usually fine, "
        "but DIFFERENT thresholds or DIFFERENT dates are NOT the same market."
    ),
    "economics": (
        "These are economic markets. Match by indicator (CPI/jobs/rates), "
        "release period, and source. Different release months are NOT the same."
    ),
}


def _build_user_prompt(kalshi_q: str, poly_q: str, category: str | None) -> str:
    hint = _CATEGORY_HINTS.get((category or "").strip().lower(), "")
    body = (
        f"Q1 (Kalshi): {kalshi_q}\n"
        f"Q2 (Polymarket): {poly_q}\n\n"
        "Do these two markets resolve to the same real-world outcome? "
        "Answer YES, NO, or MAYBE."
    )
    if hint:
        return f"{hint}\n\n{body}"
    return body

# ─── Backend detection ────────────────────────────────────────────────────────

def _detect_backend() -> str:
    """Detect which backend to use: 'cli', 'http', or 'api'.

    Priority:
    1. LLM_VERIFIER_BACKEND env var (explicit override: 'cli', 'http', 'api')
    2. If LLM_VERIFIER_HTTP_URL is set → 'http' (sidecar service on host)
    3. If ANTHROPIC_API_KEY is set → 'api'
    4. If `claude` CLI is on PATH → 'cli'
    5. Fallback → 'api' (will fail gracefully with MAYBE)
    """
    explicit = os.environ.get("LLM_VERIFIER_BACKEND", "").strip().lower()
    if explicit in ("cli", "http", "api"):
        return explicit

    if os.environ.get("LLM_VERIFIER_HTTP_URL", "").strip():
        return "http"

    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "api"

    # Check for claude CLI
    claude_path = _find_claude_cli()
    if claude_path:
        return "cli"

    return "api"


def _find_claude_cli() -> str | None:
    """Find the claude CLI binary. Checks common locations."""
    # Check PATH first
    found = shutil.which("claude")
    if found:
        return found

    # Check common macOS install locations
    common_paths = [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        os.path.expanduser("~/.claude/bin/claude"),
    ]
    for path in common_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None


# ─── Response parser ──────────────────────────────────────────────────────────

_ANSWER_RE = re.compile(r"\b(YES|NO|MAYBE)\b", re.IGNORECASE)
# Fallback: catch YES/NO/MAYBE at the start of text even without word boundary
# (e.g., "YESBoth questions..." from CLI output with no space)
_ANSWER_START_RE = re.compile(r"^(YES|NO|MAYBE)", re.IGNORECASE)


def _parse_answer(text: str) -> Literal["YES", "NO", "MAYBE"]:
    """Extract YES/NO/MAYBE from model response text. Returns MAYBE on ambiguity."""
    text = text.strip()
    # Try word-boundary match first (most reliable)
    matches = _ANSWER_RE.findall(text)
    if matches:
        first = matches[0].upper()
        if first in ("YES", "NO", "MAYBE"):
            return first  # type: ignore[return-value]
    # Fallback: check if response starts with YES/NO/MAYBE (no space after)
    start_match = _ANSWER_START_RE.match(text)
    if start_match:
        return start_match.group(1).upper()  # type: ignore[return-value]
    return "MAYBE"


# ─── CLI backend ─────────────────────────────────────────────────────────────

async def _verify_cli(
    kalshi_question: str,
    poly_question: str,
    category: str | None = None,
) -> Literal["YES", "NO", "MAYBE"]:
    """Call Claude Code CLI with --print for non-interactive verification."""
    claude_path = _find_claude_cli()
    if not claude_path:
        logger.warning("llm_verifier: claude CLI not found on PATH")
        return "MAYBE"

    prompt = f"{_SYSTEM_PROMPT}\n\n{_build_user_prompt(kalshi_question, poly_question, category)}"

    try:
        # Ensure PATH includes common install locations for screen/cron contexts
        env = os.environ.copy()
        extra_paths = [
            os.path.expanduser("~/.local/bin"),
            "/usr/local/bin",
            os.path.expanduser("~/.claude/bin"),
        ]
        current_path = env.get("PATH", "")
        env["PATH"] = ":".join(extra_paths) + ":" + current_path

        proc = await asyncio.create_subprocess_exec(
            claude_path,
            "--print",
            "--model", _CLI_MODEL,
            "--max-turns", "1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # Opus with extended thinking can take longer than Haiku
        cli_timeout = float(os.environ.get("LLM_VERIFIER_CLI_TIMEOUT", "120"))
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=cli_timeout,
        )
        text = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(
                "llm_verifier CLI returned code %d: %s",
                proc.returncode,
                err_text[:200],
            )
            return "MAYBE"

        if not text:
            logger.warning("llm_verifier CLI returned empty output")
            return "MAYBE"

        result = _parse_answer(text)
        logger.debug(
            "llm_verifier CLI result=%s raw=%s",
            result,
            text[:100],
        )
        return result

    except asyncio.TimeoutError:
        logger.warning("llm_verifier CLI timed out after %ss", os.environ.get("LLM_VERIFIER_CLI_TIMEOUT", "120"))
        return "MAYBE"
    except Exception as exc:
        logger.warning(
            "llm_verifier CLI error (returning MAYBE): %s: %s",
            type(exc).__name__,
            exc,
        )
        return "MAYBE"


# ─── API backend ─────────────────────────────────────────────────────────────

def _get_client():
    """Build an AsyncAnthropic client. Factored out so tests can patch it."""
    import anthropic
    return anthropic.AsyncAnthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )


async def _verify_api(
    kalshi_question: str,
    poly_question: str,
    category: str | None = None,
) -> Literal["YES", "NO", "MAYBE"]:
    """Call Anthropic API directly (requires ANTHROPIC_API_KEY)."""
    try:
        client = _get_client()
        resp = await client.messages.create(
            model=_API_MODEL,
            max_tokens=64,
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
                    "content": _build_user_prompt(kalshi_question, poly_question, category),
                }
            ],
        )
        text = resp.content[0].text if resp.content else ""
        return _parse_answer(text)

    except Exception as exc:
        logger.warning(
            "llm_verifier API error (returning MAYBE): %s: %s",
            type(exc).__name__,
            exc,
        )
        return "MAYBE"


# ─── HTTP backend (calls host-side verifier service) ─────────────────────────

async def _verify_http(
    kalshi_question: str,
    poly_question: str,
    category: str | None = None,
) -> Literal["YES", "NO", "MAYBE"]:
    """Call the host-side LLM verifier HTTP service."""
    import aiohttp

    url = os.environ.get("LLM_VERIFIER_HTTP_URL", "http://host.docker.internal:8079/verify")
    payload = {"kalshi_question": kalshi_question, "poly_question": poly_question}
    if category:
        payload["category"] = category

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=130),
            ) as resp:
                if resp.status != 200:
                    logger.warning("llm_verifier HTTP error: status=%d", resp.status)
                    return "MAYBE"
                data = await resp.json()
                result = data.get("result", "MAYBE").upper()
                if result in ("YES", "NO", "MAYBE"):
                    logger.debug("llm_verifier HTTP result=%s", result)
                    return result  # type: ignore[return-value]
                return "MAYBE"
    except Exception as exc:
        logger.warning(
            "llm_verifier HTTP error (returning MAYBE): %s: %s",
            type(exc).__name__,
            exc,
        )
        return "MAYBE"


# ─── Public API ───────────────────────────────────────────────────────────────

# Cached backend selection — re-evaluated per call so tests can flip env vars
# and so monkey-patched _get_client is exercised even when the system has the
# Claude CLI installed. Module load logs the initial pick for visibility.
_BACKEND = _detect_backend()
logger.info("llm_verifier initial backend: %s", _BACKEND)


def _select_backend() -> str:
    """Return current backend, allowing test code to override via env var."""
    return _detect_backend()


async def verify(
    kalshi_question: str,
    poly_question: str,
    category: str | None = None,
) -> Literal["YES", "NO", "MAYBE"]:
    """Layer 2 — ask Claude whether two markets resolve to the same outcome.

    Auto-selects between CLI (Max subscription) and API backends. Optional
    ``category`` (e.g. "sports", "politics", "crypto") enriches the prompt
    with category-specific guidance — see ``_CATEGORY_HINTS``.

    Cache key: order-independent hash of ``(kalshi_question, poly_question)``.
    Verdicts are persisted to a JSON file so a container restart doesn't
    burn LLM calls re-checking pairs we already verified.

    On any failure returns MAYBE (fail-safe — the auto-promote gate treats
    MAYBE as "not-YES", so errors never accidentally promote).
    """
    persistent_hit = _cache_get(kalshi_question, poly_question)
    if persistent_hit is not None:
        logger.debug("llm_verifier persistent cache hit for pair")
        return persistent_hit

    cache_key = frozenset({kalshi_question, poly_question})
    if cache_key in _cache:
        logger.debug("llm_verifier in-memory cache hit for pair")
        return _cache[cache_key]

    backend = _select_backend()
    if backend == "cli":
        result = await _verify_cli(kalshi_question, poly_question, category=category)
    elif backend == "http":
        result = await _verify_http(kalshi_question, poly_question, category=category)
    else:
        result = await _verify_api(kalshi_question, poly_question, category=category)

    # In-memory cache (FIFO eviction)
    if len(_cache) >= _CACHE_MAX:
        oldest_key = next(iter(_cache))
        del _cache[oldest_key]
    _cache[cache_key] = result
    # Persistent cache (flushes async-style every N writes)
    _cache_put(kalshi_question, poly_question, result)

    return result
