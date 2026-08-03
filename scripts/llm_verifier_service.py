#!/usr/bin/env python3
"""
Lightweight HTTP service that wraps Claude Code CLI for LLM verification.

Runs on the HOST machine (not Docker) and exposes a simple HTTP endpoint
that the Docker-ized Arbiter can call for market-pair verification.

Usage:
    python scripts/llm_verifier_service.py [--port 8079] [--model opus]

Endpoint:
    POST /verify
    Body: {"kalshi_question": "...", "poly_question": "..."}
    Response: {"result": "YES|NO|MAYBE", "raw": "...", "reviews": {model: verdict}}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Literal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("llm_verifier_service")

_SYSTEM_PROMPT = (
    "You are a prediction-market resolution expert. "
    "Your task is to determine whether two prediction-market questions "
    "resolve to the same real-world outcome. "
    "Answer with exactly one word on the first line: YES, NO, or MAYBE. "
    "Then optionally add a brief one-sentence reason.\n\n"
    "Rules:\n"
    "- YES: Both questions will be resolved by the exact same real-world event "
    "at the same time. Minor phrasing differences (team abbreviations like "
    "BAR/FCB or ATX/AUS, platform-specific titling) do NOT block a YES if the "
    "underlying outcome is the same.\n"
    "- NO: Different events, different time windows, different scopes "
    "(e.g. game vs series, single price vs ranking), or conflicting criteria.\n"
    "- MAYBE: Critical detail missing (e.g. ambiguous date), but visible "
    "criteria are consistent.\n\n"
    "Common mappings to recognize:\n"
    "- 'Will Team A win on YYYY-MM-DD' ≡ '<sport>-A-B-YYYY-MM-DD' for the SAME team.\n"
    "- 'Will Party X win the Senate in 2026' ≡ 'usse-midterms-2026-11-03-x'.\n"
    "- 'BTC above $X by Y' from one venue ≡ 'will Bitcoin reach $X by Y' on the other.\n\n"
    "Examples:\n"
    "Q1: Will Houston win on 2026-04-29?\n"
    "Q2: aec-mlb-hou-bal-2026-04-29\n"
    "Answer: YES - same MLB game, same team.\n\n"
    "Q1: Will Lakers win the series?\n"
    "Q2: Will Lakers win the game on 2026-04-29?\n"
    "Answer: NO - one is a series, the other a single game.\n\n"
    "Q1: Will BTC be above $100K on 2026-12-31?\n"
    "Q2: Will Bitcoin be the best-performing asset of 2026?\n"
    "Answer: NO - price threshold vs relative ranking are different."
)

_CATEGORY_HINTS = {
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

_ANSWER_RE = re.compile(r"\b(YES|NO|MAYBE)\b", re.IGNORECASE)
_ANSWER_START_RE = re.compile(r"^(YES|NO|MAYBE)", re.IGNORECASE)

_CACHE: dict[frozenset, str] = {}
# Two INDEPENDENT reviewers (operator directive 2026-07-31): every verdict
# that can gate auto-trading is answered by both models separately, and the
# consensus fails closed — YES only when BOTH say YES, NO when either says
# NO, MAYBE otherwise. Different model families catch different failure
# modes (the July party-swap mis-mapping is exactly the class a single
# reviewer waved through).
_MODELS = ["claude-opus-5", "claude-sonnet-5"]
_CLAUDE_PATH = None
# Cache namespace: verdicts from the single-reviewer era must not satisfy
# dual-review lookups, so the persistent key carries the reviewer roster.
_CACHE_VERSION = "v2"


def _consensus(answers: list[str]) -> str:
    """Fail-closed combination of independent reviewer verdicts."""
    if any(a == "NO" for a in answers):
        return "NO"
    if answers and all(a == "YES" for a in answers):
        return "YES"
    return "MAYBE"

# Persistent on-disk cache so a restart doesn't burn LLM calls re-checking
# pairs we've already verified. Path overridable via env var.
_PERSIST_PATH = os.path.expanduser(
    os.environ.get("LLM_VERIFIER_SIDECAR_CACHE", "~/.cache/arbiter_llm_verifier_cache.json")
)
_PERSISTENT: dict[str, str] = {}
_DIRTY_COUNT = 0


def _persistent_key(a: str, b: str) -> str:
    roster = ",".join(_MODELS)
    return f"{_CACHE_VERSION}|{roster}|" + "|".join(
        sorted([(a or "").strip(), (b or "").strip()])
    )


def _load_persistent_cache():
    if not os.path.exists(_PERSIST_PATH):
        return {}
    try:
        with open(_PERSIST_PATH) as f:
            return json.load(f) or {}
    except Exception as exc:
        logger.warning("could not load cache: %s", exc)
        return {}


def _persist_cache(entries: dict):
    try:
        os.makedirs(os.path.dirname(_PERSIST_PATH), exist_ok=True)
        tmp = _PERSIST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f)
        os.replace(tmp, _PERSIST_PATH)
    except Exception as exc:
        logger.warning("could not persist cache: %s", exc)


def _find_claude():
    found = shutil.which("claude")
    if found:
        return found
    for p in [
        os.path.expanduser("~/.bun/bin/claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _parse_answer(text: str) -> str:
    text = text.strip()
    matches = _ANSWER_RE.findall(text)
    if matches:
        return matches[0].upper()
    start_match = _ANSWER_START_RE.match(text)
    if start_match:
        return start_match.group(1).upper()
    return "MAYBE"


def _parse_batch_answers(text: str, expected_count: int) -> list[str]:
    results = ["MAYBE"] * expected_count
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
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        answer = str(item.get("answer", "MAYBE")).upper()
        if 0 <= idx < expected_count and answer in ("YES", "NO", "MAYBE"):
            results[idx] = answer
    return results


def _ask_model(model: str, prompt: str, timeout: int = 180) -> tuple[str, str]:
    """One reviewer's verdict. Any failure is MAYBE (fail closed)."""
    try:
        result = subprocess.run(
            [_CLAUDE_PATH, "--print", "--model", model, "--max-turns", "1"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        raw = result.stdout.strip()
        if result.returncode != 0:
            logger.warning("CLI error (%s): %s", model, result.stderr[:200])
            return "MAYBE", result.stderr[:200]
        return _parse_answer(raw), raw
    except Exception as e:
        logger.warning("Error (%s): %s", model, e)
        return "MAYBE", str(e)


def _ask_all_models(prompt: str, timeout: int = 180) -> dict[str, tuple[str, str]]:
    """Run every reviewer concurrently; returns {model: (answer, raw)}."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(_MODELS)) as pool:
        futures = {m: pool.submit(_ask_model, m, prompt, timeout) for m in _MODELS}
        return {m: f.result() for m, f in futures.items()}


def _verify_sync(
    kalshi_q: str, poly_q: str, category: str | None = None
) -> tuple[str, str, dict[str, str]]:
    global _DIRTY_COUNT
    pk = _persistent_key(kalshi_q, poly_q)
    if pk in _PERSISTENT:
        return _PERSISTENT[pk], "(persistent cached)", {}

    cache_key = frozenset({kalshi_q, poly_q})
    if cache_key in _CACHE:
        return _CACHE[cache_key], "(in-mem cached)", {}

    hint = _CATEGORY_HINTS.get((category or "").strip().lower(), "")
    user_body = (
        f"Q1 (Kalshi): {kalshi_q}\n"
        f"Q2 (Polymarket): {poly_q}\n\n"
        "Do these two markets resolve to the same real-world outcome? "
        "Answer YES, NO, or MAYBE."
    )
    user_block = f"{hint}\n\n{user_body}" if hint else user_body
    prompt = f"{_SYSTEM_PROMPT}\n\n{user_block}"

    reviews = _ask_all_models(prompt)
    answers = {m: a for m, (a, _raw) in reviews.items()}
    answer = _consensus(list(answers.values()))
    if len(set(answers.values())) > 1:
        logger.info("Reviewer disagreement %s -> %s: %s", answers, answer, kalshi_q[:60])
    raw = " | ".join(f"{m}: {a}" for m, a in answers.items())

    _CACHE[cache_key] = answer
    _PERSISTENT[pk] = answer
    _DIRTY_COUNT += 1
    # Persist every 25 writes so we don't thrash the disk.
    if _DIRTY_COUNT % 25 == 0:
        _persist_cache(dict(_PERSISTENT))
    return answer, raw, answers


def _verify_batch_sync(pairs: list[tuple[str, str]], category: str | None = None) -> tuple[list[str], str]:
    global _DIRTY_COUNT
    results: list[str | None] = [None] * len(pairs)
    missing: list[tuple[int, str, str, str]] = []

    for idx, (kalshi_q, poly_q) in enumerate(pairs):
        pk = _persistent_key(kalshi_q, poly_q)
        if pk in _PERSISTENT:
            results[idx] = _PERSISTENT[pk]
            continue
        cache_key = frozenset({kalshi_q, poly_q})
        if cache_key in _CACHE:
            results[idx] = _CACHE[cache_key]
            continue
        missing.append((idx, kalshi_q, poly_q, pk))

    if missing:
        hint = _CATEGORY_HINTS.get((category or "").strip().lower(), "")
        # Number pairs with LOCAL indices 0..len(missing)-1 — the parser
        # maps returned {"index": i} into a len(missing)-sized array, so
        # numbering with ORIGINAL request indices misassigned verdicts
        # whenever any pair ahead of them was cache-served (found by the
        # 2026-08-02 adversarial review: wrong-pair YES poisoning).
        numbered = "\n".join(
            f"{local_idx}. Q1 (Kalshi): {kalshi_q}\n   Q2 (Polymarket): {poly_q}"
            for local_idx, (_orig, kalshi_q, poly_q, _pk) in enumerate(missing)
        )
        user_block = (
            "For each indexed pair, decide whether the two markets resolve "
            "to the exact same real-world outcome. Respond only as JSON: "
            "[{\"index\":0,\"answer\":\"YES|NO|MAYBE\",\"reason\":\"short\"}, ...].\n\n"
            f"{numbered}"
        )
        if hint:
            user_block = f"{hint}\n\n{user_block}"
        prompt = f"{_SYSTEM_PROMPT}\n\n{user_block}"
        try:
            # Each reviewer grades the whole batch independently; per-index
            # consensus fails closed exactly like the single-pair path.
            reviews = _ask_all_models(prompt, timeout=240)
            per_model: dict[str, list[str]] = {}
            for model, (_answer, raw_text) in reviews.items():
                per_model[model] = _parse_batch_answers(raw_text, len(missing))
            for local_idx in range(len(missing)):
                result_idx, kalshi_q, poly_q, pk = missing[local_idx]
                votes = [per_model[m][local_idx] for m in per_model]
                answer = _consensus(votes)
                results[result_idx] = answer
                _CACHE[frozenset({kalshi_q, poly_q})] = answer
                _PERSISTENT[pk] = answer
                _DIRTY_COUNT += 1
            if _DIRTY_COUNT:
                _persist_cache(dict(_PERSISTENT))
            raw = " | ".join(
                f"{m}: {','.join(v)}" for m, v in per_model.items()
            )
            return [r or "MAYBE" for r in results], raw[:500]
        except Exception as exc:
            logger.warning("Batch error: %s", exc)
            for result_idx, *_ in missing:
                results[result_idx] = "MAYBE"
            return [r or "MAYBE" for r in results], str(exc)

    return [r or "MAYBE" for r in results], "(cached)"


class VerifyHandler(BaseHTTPRequestHandler):
    # Socket-level timeout so a wedged client can't hold the single-threaded
    # server hostage (review finding 2026-08-03).
    timeout = 30

    def _authorized(self) -> bool:
        """Require the shared token when configured (the service binds
        0.0.0.0 so containers can reach it via host.docker.internal —
        the token gates every verdict-issuing request)."""
        expected = os.environ.get("LLM_VERIFIER_TOKEN", "").strip()
        if not expected:
            return True  # unset = open (dev); prod sets it in both places
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {expected}"

    def do_POST(self):
        if self.path not in {"/verify", "/verify_batch"}:
            self.send_response(404)
            self.end_headers()
            return
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}')
            return

        if self.path == "/verify_batch":
            pairs_raw = data.get("pairs", [])
            if not isinstance(pairs_raw, list):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"pairs must be a list"}')
                return
            pairs: list[tuple[str, str]] = []
            for item in pairs_raw[:20]:
                if not isinstance(item, dict):
                    continue
                k = item.get("kalshi_question", "")
                p = item.get("poly_question", "")
                if k and p:
                    pairs.append((k, p))
            if not pairs:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"missing pairs"}')
                return
            category = data.get("category")
            logger.info("Batch verifying %d pair(s) [%s]", len(pairs), category or "-")
            results, raw = _verify_batch_sync(pairs, category=category)
            logger.info("Batch results: %s", {r: results.count(r) for r in set(results)})
            response = json.dumps({"results": results, "raw": raw[:200]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.encode())
            return

        kalshi_q = data.get("kalshi_question", "")
        poly_q = data.get("poly_question", "")
        category = data.get("category")
        if not kalshi_q or not poly_q:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"missing questions"}')
            return

        logger.info("Verifying [%s]: %s vs %s", category or "-", kalshi_q[:50], poly_q[:50])
        result, raw, reviews = _verify_sync(kalshi_q, poly_q, category=category)
        logger.info("Result: %s (%s)", result, raw[:120])

        response = json.dumps({"result": result, "raw": raw[:200], "reviews": reviews})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response.encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "reviewers": _MODELS,
                "cache_entries": len(_PERSISTENT),
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


def main():
    global _MODELS, _CLAUDE_PATH, _PERSISTENT

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8079)
    parser.add_argument(
        "--models",
        default="claude-opus-5,claude-sonnet-5",
        help="Comma-separated reviewer roster; every verdict is the "
        "fail-closed consensus of ALL listed models.",
    )
    # Back-compat: the old single-model flag becomes a one-model roster.
    parser.add_argument("--model", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    roster = args.model if args.model else args.models
    _MODELS = [m.strip() for m in roster.split(",") if m.strip()]
    _CLAUDE_PATH = _find_claude()
    if not _CLAUDE_PATH:
        print("ERROR: claude CLI not found")
        sys.exit(1)

    _PERSISTENT = _load_persistent_cache()

    print(f"LLM Verifier Service starting on port {args.port}")
    print(f"Reviewers ({len(_MODELS)}): {', '.join(_MODELS)}")
    print(f"Claude CLI: {_CLAUDE_PATH}")
    print(f"Persistent cache: {_PERSIST_PATH} ({len(_PERSISTENT)} entries loaded)")

    server = HTTPServer(("0.0.0.0", args.port), VerifyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
