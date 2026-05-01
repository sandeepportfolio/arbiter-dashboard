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
from typing import Literal

logger = logging.getLogger("arbiter.mapping.llm_verifier")

# ─── In-memory cache ──────────────────────────────────────────────────────────
_cache: dict[frozenset, Literal["YES", "NO", "MAYBE"]] = {}
_CACHE_MAX = 10_000

# ─── Model ────────────────────────────────────────────────────────────────────
_API_MODEL = "claude-sonnet-4-6"  # Fallback API model for better accuracy
_CLI_MODEL = "claude-opus-4-7"  # CLI model — uses Max subscription's Opus 4.7 for highest accuracy

# ─── Prompt ───────────────────────────────────────────────────────────────────
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


def _cache_key(kalshi_question: str, poly_question: str) -> frozenset:
    return frozenset({kalshi_question, poly_question})


def _remember(cache_key: frozenset, result: Literal["YES", "NO", "MAYBE"]) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest_key = next(iter(_cache))
        del _cache[oldest_key]
    _cache[cache_key] = result


# ─── CLI backend ─────────────────────────────────────────────────────────────

async def _verify_cli(
    kalshi_question: str,
    poly_question: str,
) -> Literal["YES", "NO", "MAYBE"]:
    """Call Claude Code CLI with --print for non-interactive verification."""
    claude_path = _find_claude_cli()
    if not claude_path:
        logger.warning("llm_verifier: claude CLI not found on PATH")
        return "MAYBE"

    prompt = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Q1 (Kalshi): {kalshi_question}\n"
        f"Q2 (Polymarket): {poly_question}\n\n"
        "Do these two markets resolve to the same real-world outcome? "
        "Answer YES, NO, or MAYBE."
    )

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
    """Build the Anthropic async client.

    Kept as a tiny helper so tests can patch the client without hitting the
    network, regardless of which backend this host auto-detects by default.
    """
    import anthropic
    return anthropic.AsyncAnthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )


async def _verify_api(
    kalshi_question: str,
    poly_question: str,
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
) -> Literal["YES", "NO", "MAYBE"]:
    """Call the host-side LLM verifier HTTP service."""
    import aiohttp

    url = os.environ.get("LLM_VERIFIER_HTTP_URL", "http://host.docker.internal:8079/verify")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"kalshi_question": kalshi_question, "poly_question": poly_question},
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


def _parse_batch_response(text: str, expected_count: int) -> list[Literal["YES", "NO", "MAYBE"]]:
    """Parse a JSON batch response; fail closed per item on malformed output."""
    results: list[Literal["YES", "NO", "MAYBE"]] = ["MAYBE"] * expected_count
    raw = text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            return results
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return results

    if not isinstance(parsed, list):
        return results

    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < expected_count:
            continue
        answer = str(item.get("answer", "MAYBE")).upper()
        if answer in ("YES", "NO", "MAYBE"):
            results[index] = answer  # type: ignore[assignment]
    return results


async def _verify_batch_api(
    pairs: list[tuple[str, str]],
) -> list[Literal["YES", "NO", "MAYBE"]]:
    """Call Anthropic once for a batch of candidate pairs."""
    try:
        client = _get_client()
        numbered = "\n".join(
            f"{idx}. Q1 (Kalshi): {kalshi_q}\n   Q2 (Polymarket): {poly_q}"
            for idx, (kalshi_q, poly_q) in enumerate(pairs)
        )
        resp = await client.messages.create(
            model=_API_MODEL,
            max_tokens=max(256, 96 * len(pairs)),
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
                        "For each indexed pair, decide whether the two markets resolve "
                        "to the exact same real-world outcome. Respond only as JSON: "
                        "[{\"index\":0,\"answer\":\"YES|NO|MAYBE\",\"reason\":\"short\"}, ...].\n\n"
                        f"{numbered}"
                    ),
                }
            ],
        )
        text = resp.content[0].text if resp.content else ""
        return _parse_batch_response(text, len(pairs))
    except Exception as exc:
        logger.warning(
            "llm_verifier batch API error (returning MAYBE): %s: %s",
            type(exc).__name__,
            exc,
        )
        return ["MAYBE"] * len(pairs)


async def _verify_batch_http(
    pairs: list[tuple[str, str]],
) -> list[Literal["YES", "NO", "MAYBE"]]:
    """Call a host-side batch verifier when available; fallback fails closed."""
    import aiohttp

    single_url = os.environ.get("LLM_VERIFIER_HTTP_URL", "http://host.docker.internal:8079/verify")
    url = single_url.rsplit("/", 1)[0] + "/verify_batch"
    payload = {
        "pairs": [
            {"kalshi_question": kalshi_q, "poly_question": poly_q}
            for kalshi_q, poly_q in pairs
        ]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status != 200:
                    return ["MAYBE"] * len(pairs)
                data = await resp.json()
                raw_results = data.get("results", [])
                results: list[Literal["YES", "NO", "MAYBE"]] = []
                for idx in range(len(pairs)):
                    value = "MAYBE"
                    if idx < len(raw_results):
                        value = str(raw_results[idx]).upper()
                    results.append(value if value in ("YES", "NO", "MAYBE") else "MAYBE")  # type: ignore[arg-type]
                return results
    except Exception as exc:
        logger.warning(
            "llm_verifier batch HTTP error (returning MAYBE): %s: %s",
            type(exc).__name__,
            exc,
        )
        return ["MAYBE"] * len(pairs)


# ─── Public API ───────────────────────────────────────────────────────────────

# Detect once at module load
_BACKEND = _detect_backend()
logger.info("llm_verifier using backend: %s", _BACKEND)


async def verify(
    kalshi_question: str,
    poly_question: str,
) -> Literal["YES", "NO", "MAYBE"]:
    """Layer 2 — ask Claude whether two markets resolve to the same outcome.

    Auto-selects between CLI (Max subscription) and API backends.
    Cache key: frozenset({kalshi_question, poly_question}).

    On any failure returns MAYBE (fail-safe — the auto-promote gate treats
    MAYBE as "not-YES", so errors never accidentally promote).
    """
    cache_key = _cache_key(kalshi_question, poly_question)

    # Check in-memory cache first
    if cache_key in _cache:
        logger.debug("llm_verifier cache hit for pair")
        return _cache[cache_key]

    if _BACKEND == "cli":
        result = await _verify_cli(kalshi_question, poly_question)
    elif _BACKEND == "http":
        result = await _verify_http(kalshi_question, poly_question)
    else:
        result = await _verify_api(kalshi_question, poly_question)

    _remember(cache_key, result)

    return result


async def verify_batch(
    pairs: list[tuple[str, str]],
    *,
    batch_size: int = 20,
) -> list[Literal["YES", "NO", "MAYBE"]]:
    """Layer 2 batch verifier with the same fail-safe/cache semantics as verify()."""
    if not pairs:
        return []

    results: list[Literal["YES", "NO", "MAYBE"] | None] = [None] * len(pairs)
    missing: list[tuple[int, tuple[str, str], frozenset]] = []

    for idx, (kalshi_question, poly_question) in enumerate(pairs):
        key = _cache_key(kalshi_question, poly_question)
        cached = _cache.get(key)
        if cached is not None:
            results[idx] = cached
        else:
            missing.append((idx, (kalshi_question, poly_question), key))

    for start in range(0, len(missing), max(1, batch_size)):
        chunk = missing[start:start + max(1, batch_size)]
        chunk_pairs = [pair for _, pair, _ in chunk]
        if _BACKEND == "api":
            chunk_results = await _verify_batch_api(chunk_pairs)
        elif _BACKEND == "http":
            chunk_results = await _verify_batch_http(chunk_pairs)
            if all(result == "MAYBE" for result in chunk_results):
                chunk_results = [await verify(*pair) for pair in chunk_pairs]
        else:
            chunk_results = [await verify(*pair) for pair in chunk_pairs]

        for (result_index, _, key), result in zip(chunk, chunk_results):
            normalized = result if result in ("YES", "NO", "MAYBE") else "MAYBE"
            results[result_index] = normalized
            _remember(key, normalized)

    return [result or "MAYBE" for result in results]
