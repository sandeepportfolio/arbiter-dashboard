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
    Response: {"result": "YES|NO|MAYBE", "raw": "..."}
"""
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
_MODEL = "claude-opus-4-7"
_CLAUDE_PATH = None

# Persistent on-disk cache so a restart doesn't burn LLM calls re-checking
# pairs we've already verified. Path overridable via env var.
_PERSIST_PATH = os.path.expanduser(
    os.environ.get("LLM_VERIFIER_SIDECAR_CACHE", "~/.cache/arbiter_llm_verifier_cache.json")
)
_PERSISTENT: dict[str, str] = {}
_DIRTY_COUNT = 0


def _persistent_key(a: str, b: str) -> str:
    return "|".join(sorted([(a or "").strip(), (b or "").strip()]))


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
    for p in [os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude"]:
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


def _verify_sync(kalshi_q: str, poly_q: str, category: str | None = None) -> tuple[str, str]:
    global _DIRTY_COUNT
    pk = _persistent_key(kalshi_q, poly_q)
    if pk in _PERSISTENT:
        return _PERSISTENT[pk], "(persistent cached)"

    cache_key = frozenset({kalshi_q, poly_q})
    if cache_key in _CACHE:
        return _CACHE[cache_key], "(in-mem cached)"

    hint = _CATEGORY_HINTS.get((category or "").strip().lower(), "")
    user_body = (
        f"Q1 (Kalshi): {kalshi_q}\n"
        f"Q2 (Polymarket): {poly_q}\n\n"
        "Do these two markets resolve to the same real-world outcome? "
        "Answer YES, NO, or MAYBE."
    )
    user_block = f"{hint}\n\n{user_body}" if hint else user_body
    prompt = f"{_SYSTEM_PROMPT}\n\n{user_block}"
    try:
        result = subprocess.run(
            [_CLAUDE_PATH, "--print", "--model", _MODEL, "--max-turns", "1"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw = result.stdout.strip()
        if result.returncode != 0:
            logger.warning("CLI error: %s", result.stderr[:200])
            return "MAYBE", result.stderr[:200]
        answer = _parse_answer(raw)
        _CACHE[cache_key] = answer
        _PERSISTENT[pk] = answer
        _DIRTY_COUNT += 1
        # Persist every 25 writes so we don't thrash the disk.
        if _DIRTY_COUNT % 25 == 0:
            _persist_cache(dict(_PERSISTENT))
        return answer, raw
    except Exception as e:
        logger.warning("Error: %s", e)
        return "MAYBE", str(e)


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
        numbered = "\n".join(
            f"{idx}. Q1 (Kalshi): {kalshi_q}\n   Q2 (Polymarket): {poly_q}"
            for idx, kalshi_q, poly_q, _ in missing
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
            result = subprocess.run(
                [_CLAUDE_PATH, "--print", "--model", _MODEL, "--max-turns", "1"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=240,
            )
            raw = result.stdout.strip()
            if result.returncode != 0:
                logger.warning("batch CLI error: %s", result.stderr[:200])
                raw = result.stderr[:200]
                batch_answers = ["MAYBE"] * len(missing)
            else:
                batch_answers = _parse_batch_answers(raw, len(missing))
            for local_idx, answer in enumerate(batch_answers):
                result_idx, kalshi_q, poly_q, pk = missing[local_idx]
                answer = answer if answer in ("YES", "NO", "MAYBE") else "MAYBE"
                results[result_idx] = answer
                _CACHE[frozenset({kalshi_q, poly_q})] = answer
                _PERSISTENT[pk] = answer
                _DIRTY_COUNT += 1
            if _DIRTY_COUNT:
                _persist_cache(dict(_PERSISTENT))
            return [r or "MAYBE" for r in results], raw[:500]
        except Exception as exc:
            logger.warning("Batch error: %s", exc)
            for result_idx, *_ in missing:
                results[result_idx] = "MAYBE"
            return [r or "MAYBE" for r in results], str(exc)

    return [r or "MAYBE" for r in results], "(cached)"


class VerifyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in {"/verify", "/verify_batch"}:
            self.send_response(404)
            self.end_headers()
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
        result, raw = _verify_sync(kalshi_q, poly_q, category=category)
        logger.info("Result: %s", result)

        response = json.dumps({"result": result, "raw": raw[:200]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response.encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


def main():
    global _MODEL, _CLAUDE_PATH, _PERSISTENT

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8079)
    parser.add_argument("--model", default="claude-opus-4-7")
    args = parser.parse_args()

    _MODEL = args.model
    _CLAUDE_PATH = _find_claude()
    if not _CLAUDE_PATH:
        print("ERROR: claude CLI not found")
        sys.exit(1)

    _PERSISTENT = _load_persistent_cache()

    print(f"LLM Verifier Service starting on port {args.port}")
    print(f"Using model: {_MODEL}")
    print(f"Claude CLI: {_CLAUDE_PATH}")
    print(f"Persistent cache: {_PERSIST_PATH} ({len(_PERSISTENT)} entries loaded)")

    server = HTTPServer(("0.0.0.0", args.port), VerifyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
