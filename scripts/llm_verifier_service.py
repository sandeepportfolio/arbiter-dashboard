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

_ANSWER_RE = re.compile(r"\b(YES|NO|MAYBE)\b", re.IGNORECASE)
_ANSWER_START_RE = re.compile(r"^(YES|NO|MAYBE)", re.IGNORECASE)

_CACHE: dict[frozenset, str] = {}
_MODEL = "claude-opus-4-7"
_CLAUDE_PATH = None


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


def _verify_sync(kalshi_q: str, poly_q: str) -> tuple[str, str]:
    cache_key = frozenset({kalshi_q, poly_q})
    if cache_key in _CACHE:
        return _CACHE[cache_key], "(cached)"

    prompt = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Q1 (Kalshi): {kalshi_q}\n"
        f"Q2 (Polymarket): {poly_q}\n\n"
        "Do these two markets resolve to the same real-world outcome? "
        "Answer YES, NO, or MAYBE."
    )
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
        return answer, raw
    except Exception as e:
        logger.warning("Error: %s", e)
        return "MAYBE", str(e)


class VerifyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/verify":
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

        kalshi_q = data.get("kalshi_question", "")
        poly_q = data.get("poly_question", "")
        if not kalshi_q or not poly_q:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"missing questions"}')
            return

        logger.info("Verifying: %s vs %s", kalshi_q[:50], poly_q[:50])
        result, raw = _verify_sync(kalshi_q, poly_q)
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
    global _MODEL, _CLAUDE_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8079)
    parser.add_argument("--model", default="claude-opus-4-7")
    args = parser.parse_args()

    _MODEL = args.model
    _CLAUDE_PATH = _find_claude()
    if not _CLAUDE_PATH:
        print("ERROR: claude CLI not found")
        sys.exit(1)

    print(f"LLM Verifier Service starting on port {args.port}")
    print(f"Using model: {_MODEL}")
    print(f"Claude CLI: {_CLAUDE_PATH}")

    server = HTTPServer(("0.0.0.0", args.port), VerifyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
