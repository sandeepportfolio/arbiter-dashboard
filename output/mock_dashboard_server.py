from __future__ import annotations

import copy
import json
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(r"C:\Users\sande\Documents\arbiter-dashboard")
PORT = 8090
NOW = 1776340800
AUTH_TOKEN = "demo-token"

SYSTEM = {
    "timestamp": NOW,
    "mode": "live",
    "tracked_markets": {
        "kalshi-btc-100k": {},
        "polymarket-fed-cut": {},
        "election-house-majority": {},
    },
    "scanner": {
        "tradable_opportunities": 7,
        "active_opportunities": 14,
        "best_edge_cents": 18.4,
        "persistence_scans": 3,
        "max_quote_age_seconds": 15,
        "published": 22,
    },
    "execution": {
        "total_pnl": 1859.48,
        "total_executions": 128,
        "audit": {"pass_rate": 0.982},
    },
    "audit": {
        "audits_run": 2048,
        "pass_rate": 0.982,
    },
    "balances": {
        "kalshi": {"timestamp": NOW - 80, "balance": 12400, "is_low": False},
        "polymarket": {"timestamp": NOW - 75, "balance": 9100, "is_low": False},
        "predictit": {"timestamp": NOW - 65, "balance": 45, "is_low": True},
    },
    "collectors": {
        "kalshi": {
            "total_fetches": 5420,
            "total_errors": 0,
            "consecutive_errors": 0,
            "rate_limiter": {"available_tokens": 118, "remaining_penalty_seconds": 0},
            "circuit": {"state": "closed"},
        },
        "polymarket": {
            "total_fetches": 5362,
            "total_errors": 2,
            "consecutive_errors": 0,
            "rate_limiter": {"available_tokens": 87, "remaining_penalty_seconds": 12},
            "clob_circuit": {"state": "half_open"},
        },
        "predictit": {
            "total_fetches": 4210,
            "total_errors": 7,
            "consecutive_errors": 1,
            "rate_limiter": {"available_tokens": 66, "remaining_penalty_seconds": 0},
            "circuit": {"state": "closed"},
        },
    },
    "counts": {
        "prices": 5200,
        "incidents": 2,
    },
    "series": {
        "scanner": [
            {"timestamp": NOW - 3600, "best_edge_cents": 7.2},
            {"timestamp": NOW - 3000, "best_edge_cents": 8.6},
            {"timestamp": NOW - 2400, "best_edge_cents": 10.1},
            {"timestamp": NOW - 1800, "best_edge_cents": 11.4},
            {"timestamp": NOW - 1200, "best_edge_cents": 13.1},
            {"timestamp": NOW - 600, "best_edge_cents": 15.9},
            {"timestamp": NOW - 120, "best_edge_cents": 18.4},
        ],
        "equity": [
            {"timestamp": NOW - 3600, "equity": 40100},
            {"timestamp": NOW - 3000, "equity": 40520},
            {"timestamp": NOW - 2400, "equity": 40980},
            {"timestamp": NOW - 1800, "equity": 41460},
            {"timestamp": NOW - 1200, "equity": 41820},
            {"timestamp": NOW - 600, "equity": 42090},
            {"timestamp": NOW - 20, "equity": 42150},
        ],
    },
}

OPPORTUNITIES = [
    {
        "canonical_id": "btc-100k-2026",
        "description": "BTC to 100k before year-end",
        "status": "tradable",
        "yes_platform": "kalshi",
        "no_platform": "polymarket",
        "yes_price": 0.44,
        "no_price": 0.37,
        "gross_edge": 0.19,
        "total_fees": 0.006,
        "net_edge": 0.184,
        "net_edge_cents": 18.4,
        "max_profit_usd": 736.0,
        "confidence": 0.94,
        "persistence_count": 3,
        "quote_age_seconds": 1.8,
        "suggested_qty": 4000,
        "min_available_liquidity": 9600,
        "mapping_status": "confirmed",
        "requires_manual": False,
        "timestamp": NOW - 20,
    },
    {
        "canonical_id": "fed-cut-july",
        "description": "Fed rate cut by July",
        "status": "manual",
        "yes_platform": "predictit",
        "no_platform": "kalshi",
        "yes_price": 0.52,
        "no_price": 0.34,
        "gross_edge": 0.14,
        "total_fees": 0.018,
        "net_edge": 0.122,
        "net_edge_cents": 12.2,
        "max_profit_usd": 590.4,
        "confidence": 0.88,
        "persistence_count": 3,
        "quote_age_seconds": 3.4,
        "suggested_qty": 2100,
        "min_available_liquidity": 4200,
        "mapping_status": "review",
        "requires_manual": True,
        "timestamp": NOW - 36,
    },
    {
        "canonical_id": "house-majority",
        "description": "House majority control",
        "status": "review",
        "yes_platform": "polymarket",
        "no_platform": "kalshi",
        "yes_price": 0.59,
        "no_price": 0.28,
        "gross_edge": 0.13,
        "total_fees": 0.009,
        "net_edge": 0.121,
        "net_edge_cents": 12.1,
        "max_profit_usd": 488.2,
        "confidence": 0.81,
        "persistence_count": 2,
        "quote_age_seconds": 5.8,
        "suggested_qty": 1800,
        "min_available_liquidity": 3500,
        "mapping_status": "review",
        "requires_manual": False,
        "timestamp": NOW - 52,
    },
    {
        "canonical_id": "oil-above-95",
        "description": "Oil above 95 in Q3",
        "status": "stale",
        "yes_platform": "kalshi",
        "no_platform": "polymarket",
        "yes_price": 0.31,
        "no_price": 0.47,
        "gross_edge": 0.07,
        "total_fees": 0.012,
        "net_edge": 0.058,
        "net_edge_cents": 5.8,
        "max_profit_usd": 119.0,
        "confidence": 0.56,
        "persistence_count": 1,
        "quote_age_seconds": 18.2,
        "suggested_qty": 900,
        "min_available_liquidity": 1600,
        "mapping_status": "candidate",
        "requires_manual": False,
        "timestamp": NOW - 90,
    },
]

TRADES = [
    {
        "arb_id": "arb-1042",
        "status": "filled",
        "timestamp": NOW - 300,
        "realized_pnl": 85.25,
        "opportunity": {"description": "BTC to 100k before year-end", "canonical_id": "btc-100k-2026"},
        "leg_yes": {"platform": "kalshi", "price": 0.47, "quantity": 140},
        "leg_no": {"platform": "predictit", "price": 0.40, "quantity": 140},
        "notes": ["filled cleanly"],
    },
    {
        "arb_id": "arb-1041",
        "status": "submitted",
        "timestamp": NOW - 900,
        "realized_pnl": 12.40,
        "opportunity": {"description": "Fed rate cut by July", "canonical_id": "fed-cut-july"},
        "leg_yes": {"platform": "predictit", "price": 0.52, "quantity": 90},
        "leg_no": {"platform": "kalshi", "price": 0.34, "quantity": 90},
        "notes": ["awaiting hedge confirmation"],
    },
    {
        "arb_id": "arb-1039",
        "status": "failed",
        "timestamp": NOW - 1600,
        "realized_pnl": -19.57,
        "opportunity": {"description": "House majority control", "canonical_id": "house-majority"},
        "leg_yes": {"platform": "polymarket", "price": 0.61, "quantity": 80},
        "leg_no": {"platform": "kalshi", "price": 0.29, "quantity": 80},
        "notes": ["hedge leg timed out"],
    },
]

MANUAL_POSITIONS = [
    {
        "position_id": "manual-201",
        "canonical_id": "fed-cut-july",
        "description": "PredictIt-assisted July rate cut route",
        "yes_platform": "predictit",
        "no_platform": "kalshi",
        "yes_price": 0.52,
        "no_price": 0.34,
        "quantity": 90,
        "status": "awaiting-entry",
        "timestamp": NOW - 420,
        "instructions": "Place the PredictIt YES leg first, verify quantity, then confirm entry in Arbiter.",
        "note": "Awaiting operator acknowledgement.",
    }
]

INCIDENTS = [
    {
        "incident_id": "inc-77",
        "status": "open",
        "severity": "critical",
        "message": "One-leg fill mismatch requires operator review",
        "canonical_id": "house-majority",
        "arb_id": "arb-1039",
        "timestamp": NOW - 240,
        "metadata": {
            "original_yes": 0.61,
            "current_yes": 0.64,
            "original_no": 0.29,
            "current_no": 0.31,
        },
        "resolution_note": "Still waiting for operator resolution.",
    },
    {
        "incident_id": "inc-65",
        "status": "resolved",
        "severity": "warning",
        "message": "Collector recovered after cooldown",
        "canonical_id": "btc-100k-2026",
        "arb_id": "arb-1038",
        "timestamp": NOW - 1800,
        "metadata": {"reason": "temporary rate-limit cooldown"},
        "resolution_note": "Auto-recovered after retry window.",
    },
]

MAPPINGS = [
    {
        "canonical_id": "btc-100k-2026",
        "description": "BTC to 100k before year-end",
        "status": "confirmed",
        "allow_auto_trade": True,
        "notes": "Confirmed mapping across both venues.",
        "kalshi": "KXBTC100K",
        "polymarket": "PM-BTC-100K",
        "predictit": "PI-BTC-100K",
    },
    {
        "canonical_id": "fed-cut-july",
        "description": "Fed rate cut by July",
        "status": "review",
        "allow_auto_trade": False,
        "review_note": "PredictIt wording still needs manual confirmation.",
        "kalshi": "KFEDCUTJUL",
        "predictit": "PI-FEDCUT-JULY",
    },
]

PORTFOLIO = {
    "total_exposure": 21400,
    "total_open_positions": 12,
    "violations": [
        {"level": "warning", "message": "Concentration on election series"},
    ],
    "by_venue": {
        "kalshi": {"platform": "kalshi", "total_exposure": 12400, "position_count": 7, "is_low_balance": False},
        "polymarket": {"platform": "polymarket", "total_exposure": 9000, "position_count": 5, "is_low_balance": False},
        "predictit": {"platform": "predictit", "total_exposure": 0, "position_count": 0, "is_low_balance": True},
    },
}

PROFITABILITY = {
    "verdict": "collecting_evidence",
    "progress": 0.68,
    "completed_executions": 128,
    "profitable_executions": 84,
    "losing_executions": 44,
    "total_realized_pnl": 1859.48,
    "audit_pass_rate": 0.982,
    "incident_rate": 0.018,
    "reasons": [
        "Need 22 more completed executions before the validator can graduate the run.",
        "PredictIt inventory still requires operator-confirmed exits.",
    ],
}

STATE = {
    "system": SYSTEM,
    "opportunities": OPPORTUNITIES,
    "trades": TRADES,
    "errors": INCIDENTS,
    "manual-positions": MANUAL_POSITIONS,
    "market-mappings": MAPPINGS,
    "portfolio": PORTFOLIO,
    "profitability": PROFITABILITY,
}


def payload(name: str):
    return copy.deepcopy(STATE[name])


def require_auth(handler: "Handler") -> bool:
    return handler.headers.get("Authorization") == f"Bearer {AUTH_TOKEN}"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/system":
            return self.send_json(payload("system"))
        if path == "/api/opportunities":
            return self.send_json(payload("opportunities"))
        if path == "/api/trades":
            return self.send_json(payload("trades"))
        if path == "/api/errors":
            return self.send_json(payload("errors"))
        if path == "/api/manual-positions":
            return self.send_json(payload("manual-positions"))
        if path == "/api/market-mappings":
            return self.send_json(payload("market-mappings"))
        if path == "/api/portfolio":
            return self.send_json(payload("portfolio"))
        if path == "/api/profitability":
            return self.send_json(payload("profitability"))
        if path == "/api/auth/me":
            if require_auth(self):
                return self.send_json({"authenticated": True, "email": "operator@arbiter.local"})
            return self.send_json({"authenticated": False, "email": ""})
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            data = self.read_json()
            return self.send_json({"token": AUTH_TOKEN, "email": data.get("email", "operator@arbiter.local")})
        if path == "/api/auth/logout":
            return self.send_json({"ok": True})
        if not require_auth(self):
            return self.send_json({"error": "unauthorized"}, status=401)

        data = self.read_json()
        if path.startswith("/api/manual-positions/"):
            position_id = path.rsplit("/", 1)[-1]
            for position in STATE["manual-positions"]:
                if position["position_id"] != position_id:
                    continue
                action = data.get("action")
                if action == "mark_entered":
                    position["status"] = "entered"
                    position["note"] = "Operator confirmed manual entry."
                elif action == "mark_closed":
                    position["status"] = "manual_closed"
                    position["note"] = "Manual route closed and reconciled."
                elif action == "cancel":
                    position["status"] = "manual_cancelled"
                    position["note"] = "Operator cancelled the manual route."
                return self.send_json({"ok": True})
            return self.send_json({"error": "not found"}, status=404)

        if path.startswith("/api/errors/"):
            incident_id = path.rsplit("/", 1)[-1]
            for incident in STATE["errors"]:
                if incident["incident_id"] == incident_id:
                    incident["status"] = "resolved"
                    incident["resolution_note"] = "Resolved from the operator desk."
                    return self.send_json({"ok": True})
            return self.send_json({"error": "not found"}, status=404)

        if path.startswith("/api/market-mappings/"):
            canonical_id = path.rsplit("/", 1)[-1]
            for mapping in STATE["market-mappings"]:
                if mapping["canonical_id"] != canonical_id:
                    continue
                action = data.get("action")
                if action == "confirm":
                    mapping["status"] = "confirmed"
                elif action == "review":
                    mapping["status"] = "review"
                elif action == "enable_auto_trade":
                    mapping["allow_auto_trade"] = True
                elif action == "disable_auto_trade":
                    mapping["allow_auto_trade"] = False
                return self.send_json({"ok": True})
            return self.send_json({"error": "not found"}, status=404)

        return self.send_json({"error": "not found"}, status=404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock dashboard server on http://127.0.0.1:{PORT}")
    server.serve_forever()
