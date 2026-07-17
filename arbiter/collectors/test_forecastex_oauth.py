"""ForecastExClient OAuth 1.0a transport wiring (Path B, zero-login).

Verifies that attaching an ``oauth`` object to the client:
  - routes every request to api.ibkr.com instead of the local gateway,
  - ensures a Live Session Token and signs each attempt (query params
    included in the signature, per OAuth 1.0a),
  - signs the iserver bridge init,
  - invalidates the LST on a 401 so the next request re-handshakes,
and that a client WITHOUT oauth behaves exactly as before (gateway URL,
no Authorization header).

Uses a stub oauth object — the real signing crypto is covered by
arbiter/auth/test_ibkr_oauth.py; here we only care about the transport
plumbing.
"""
from __future__ import annotations

import pytest
from aioresponses import aioresponses

from arbiter.collectors.forecastex import ForecastExClient

GATEWAY = "https://localhost:5000/v1/api"
API_BASE = "https://api.ibkr.com/v1/api"
ACCOUNT = "DU1234567"


class _StubOAuth:
    api_base = API_BASE

    def __init__(self):
        self.ensured = 0
        self.invalidated = 0
        self.signed: list[tuple[str, str, dict | None]] = []

    async def ensure_live_session_token(self, session):
        self.ensured += 1
        return "lst-b64"

    def auth_header(self, method, url, extra_oauth=None, query_params=None):
        self.signed.append((method, url, query_params))
        return 'OAuth realm="limited_poa", oauth_signature="stub"'

    def invalidate(self):
        self.invalidated += 1


def _oauth_client() -> tuple[ForecastExClient, _StubOAuth]:
    client = ForecastExClient(
        gateway_url=GATEWAY,
        account_id=ACCOUNT,
        verify_ssl=False,
        paper_trading=True,
    )
    stub = _StubOAuth()
    client.oauth = stub
    return client, stub


def test_url_uses_api_base_when_oauth_attached():
    client, _ = _oauth_client()
    assert client._url("/portfolio/accounts") == f"{API_BASE}/portfolio/accounts"


def test_url_uses_gateway_without_oauth():
    client = ForecastExClient(
        gateway_url=GATEWAY, account_id=ACCOUNT, verify_ssl=False,
        paper_trading=True,
    )
    assert client._url("/portfolio/accounts") == f"{GATEWAY}/portfolio/accounts"


@pytest.mark.asyncio
async def test_oauth_mode_signs_request_and_hits_api_base():
    client, stub = _oauth_client()
    try:
        with aioresponses() as m:
            m.get(f"{API_BASE}/portfolio/accounts", payload={"ok": True})
            result = await client._request("GET", "/portfolio/accounts")
        assert result == {"ok": True}
        assert stub.ensured == 1
        method, url, query = stub.signed[0]
        assert (method, url) == ("GET", f"{API_BASE}/portfolio/accounts")
        # The signed Authorization header must actually go out on the wire.
        sent = list(m.requests.values())[0][0]
        assert sent.kwargs["headers"]["Authorization"].startswith("OAuth ")
        assert sent.kwargs["headers"]["User-Agent"] == "arbiter-ibkr-oauth/1.0"
    finally:
        if client.session:
            await client.session.close()


@pytest.mark.asyncio
async def test_oauth_mode_passes_query_params_to_signer():
    client, stub = _oauth_client()
    try:
        with aioresponses() as m:
            m.get(
                f"{API_BASE}/iserver/secdef/search?symbol=FF",
                payload={"ok": True},
            )
            # /iserver path would trigger bridge init; mark it ready to keep
            # this test focused on query-param signing.
            client._bridge_ready = True
            await client._request(
                "GET", "/iserver/secdef/search", params={"symbol": "FF"}
            )
        assert stub.signed[0][2] == {"symbol": "FF"}
    finally:
        if client.session:
            await client.session.close()


@pytest.mark.asyncio
async def test_gateway_mode_sends_no_authorization_header():
    client = ForecastExClient(
        gateway_url=GATEWAY, account_id=ACCOUNT, verify_ssl=False,
        paper_trading=True,
    )
    try:
        with aioresponses() as m:
            m.get(f"{GATEWAY}/portfolio/accounts", payload={"ok": True})
            await client._request("GET", "/portfolio/accounts")
            sent = list(m.requests.values())[0][0]
            assert "Authorization" not in sent.kwargs["headers"]
    finally:
        if client.session:
            await client.session.close()


@pytest.mark.asyncio
async def test_oauth_401_invalidates_lst():
    client, stub = _oauth_client()
    try:
        with aioresponses() as m:
            m.get(f"{API_BASE}/portfolio/accounts", status=401, body="expired")
            with pytest.raises(RuntimeError, match="re-handshakes"):
                await client._request("GET", "/portfolio/accounts")
        assert stub.invalidated == 1
    finally:
        if client.session:
            await client.session.close()


@pytest.mark.asyncio
async def test_oauth_bridge_init_is_signed():
    client, stub = _oauth_client()
    try:
        with aioresponses() as m:
            m.post(f"{API_BASE}/iserver/auth/ssodh/init", payload={"authenticated": True})
            m.get(f"{API_BASE}/iserver/secdef/search", payload={"ok": True})
            await client._request("GET", "/iserver/secdef/search")
        init_calls = [s for s in stub.signed
                      if s[1].endswith("/iserver/auth/ssodh/init")]
        assert len(init_calls) == 1
        assert init_calls[0][0] == "POST"
        assert client._bridge_ready is True
        post_reqs = [v for k, v in m.requests.items() if k[0] == "POST"]
        assert post_reqs, "bridge init POST never sent"
        headers = post_reqs[0][0].kwargs["headers"]
        assert headers["Authorization"].startswith("OAuth ")
    finally:
        if client.session:
            await client.session.close()
