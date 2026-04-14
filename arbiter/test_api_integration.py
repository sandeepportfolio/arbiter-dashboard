import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import aiohttp


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_server(port: int, timeout: float = 15.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise AssertionError(f"Server on port {port} did not become ready")


def test_api_and_dashboard_contracts():
    port = free_port()
    env = dict(os.environ)
    env["ARBITER_UI_SMOKE_SEED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "arbiter.main", "--api-only", "--port", str(port)],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        wait_for_server(port)

        def get_json(path: str):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        def post_json(path: str, payload: dict):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            public_html = response.read().decode("utf-8")
        assert "ARBITER - Prediction Market Arbitrage Dashboard" in public_html
        assert 'id="loginScreen"' in public_html

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ops", timeout=5) as response:
            ops_html = response.read().decode("utf-8")
        assert "ARBITER LIVE" in ops_html
        assert "Execution equity" in ops_html

        health = get_json("/api/health")
        assert health["status"] == "ok"

        system = get_json("/api/system")
        assert system["mode"] == "dry-run"
        assert "scanner" in system
        assert "execution" in system
        assert "counts" in system
        assert "series" in system

        assert isinstance(get_json("/api/opportunities"), list)
        assert isinstance(get_json("/api/trades"), list)
        assert isinstance(get_json("/api/errors"), list)
        assert isinstance(get_json("/api/manual-positions"), list)
        assert len(get_json("/api/manual-positions")) >= 2
        assert len(get_json("/api/errors")) >= 1

        mappings = get_json("/api/market-mappings")
        assert isinstance(mappings, list)
        assert len(mappings) >= 1
        assert all("canonical_id" in row for row in mappings)
        assert any(row["canonical_id"] == "DEM_HOUSE_2026" for row in mappings)

        manual_positions = get_json("/api/manual-positions")
        entered = post_json(f"/api/manual-positions/{manual_positions[0]['position_id']}", {"action": "mark_entered"})
        assert entered["status"] == "entered"

        incidents = get_json("/api/errors")
        resolved = post_json(f"/api/errors/{incidents[0]['incident_id']}", {"action": "resolve"})
        assert resolved["status"] == "resolved"

        mapping_update = post_json("/api/market-mappings/DEM_HOUSE_2026", {"action": "confirm"})
        assert mapping_update["status"] == "confirmed"
        auto_trade_enabled = post_json("/api/market-mappings/DEM_HOUSE_2026", {"action": "enable_auto_trade"})
        assert auto_trade_enabled["status"] == "confirmed"
        assert auto_trade_enabled["allow_auto_trade"] is True

        async def check_ws():
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                    message = await ws.receive(timeout=5)
                    assert message.type == aiohttp.WSMsgType.TEXT
                    payload = json.loads(message.data)
                    assert payload["type"] == "bootstrap"
                    assert "payload" in payload
                    await ws.send_json({"action": "ping"})
                    pong = await ws.receive(timeout=5)
                    pong_payload = json.loads(pong.data)
                    assert pong_payload["type"] == "heartbeat"

        asyncio.run(check_ws())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
