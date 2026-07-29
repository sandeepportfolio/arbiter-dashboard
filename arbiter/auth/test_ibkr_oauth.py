"""Unit tests for IBKR OAuth 1.0a. The DH/LST handshake is simulated end-to-end
(this test plays both the client and the IBKR server) so the crypto is verified
without a live IBKR endpoint. Only the final network round-trip needs the real
consumer key, which arrives ~1-2 weeks post-registration."""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arbiter.auth.ibkr_oauth import IbkrOAuth1a, _to_byte_array


# Small but real DH group (RFC 3526 1536-bit MODP would be slow to gen; use a
# fixed known prime — the math is identical at any size).
_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A63A3620FFFFFFFFFFFFFFFF", 16,
)
_G = 2


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_auth(raw_secret: bytes = b"super-secret-token-material-xyz"):
    sig = _rsa()
    enc = _rsa()
    sig_pem = sig.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    enc_pem = enc.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    # access_token_secret "as issued": raw secret RSA-encrypted with the enc pubkey.
    enc_secret_b64 = base64.b64encode(
        enc.public_key().encrypt(raw_secret, padding.PKCS1v15())
    ).decode()
    auth = IbkrOAuth1a(
        consumer_key="ARBITER01",
        access_token="atok123",
        access_token_secret=enc_secret_b64,
        signature_key_pem=sig_pem,
        encryption_key_pem=enc_pem,
        dh_prime=_P,
        dh_generator=_G,
    )
    return auth, sig, raw_secret


def _der_int(n: int) -> bytes:
    body = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    return b"\x02" + _der_len(len(body)) + body


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def test_read_dh_params_parses_pkcs3_pem_without_openssl(tmp_path):
    """cryptography >= 49 raises 'Invalid DH parameters' on the exact PKCS#3
    file registered with IBKR (which therefore can never be regenerated), so
    the loader must decode the ASN.1 SEQUENCE itself. Seen live 2026-07-29:
    host cryptography 46 parsed dhparam.pem, the container's 49.0.0 crash-
    looped the whole API on boot."""
    seq_body = _der_int(_P) + _der_int(2)
    der = b"\x30" + _der_len(len(seq_body)) + seq_body
    b64 = base64.encodebytes(der).decode()
    pem = f"-----BEGIN DH PARAMETERS-----\n{b64}-----END DH PARAMETERS-----\n"
    path = tmp_path / "dhparam.pem"
    path.write_text(pem)
    p, g = IbkrOAuth1a._read_dh_params(str(path))
    assert p == _P
    assert g == 2


def test_signature_base_string_is_sorted_and_encoded():
    auth, _, _ = _make_auth()
    base = auth.signature_base_string(
        "get", "https://api.ibkr.com/v1/api/x",
        {"b": "2", "a": "1 2"},
    )
    # method upper-cased, URL percent-encoded, params sorted + double-encoded
    assert base.startswith("GET&")
    assert "https%3A%2F%2Fapi.ibkr.com" in base
    # deterministic ordering: 'a' encoded param appears before 'b'
    assert base.index("a%3D") < base.index("b%3D")


def test_rsa_sha256_signature_verifies_with_public_key():
    from urllib.parse import unquote
    auth, sig_key, _ = _make_auth()
    base = "POST&https%3A%2F%2Fx&params"
    sig_hdr = auth._rsa_sha256_sign(base)
    raw = base64.b64decode(unquote(sig_hdr))
    # Must verify against the signing public key (no exception = valid).
    from cryptography.hazmat.primitives import hashes
    sig_key.public_key().verify(raw, base.encode(), padding.PKCS1v15(), hashes.SHA256())


def test_decrypt_access_token_secret_roundtrips():
    auth, _, raw = _make_auth(b"the-raw-lst-prepend-bytes")
    assert auth._decrypt_access_token_secret() == raw


def test_live_session_token_handshake_end_to_end():
    """Simulate the full DH handshake: client builds request, 'IBKR' responds,
    client derives the LST, and it validates against the server's signature."""
    auth, _, raw_secret = _make_auth(b"prepend-secret-42")
    _url, _headers, a = auth.build_lst_request()

    # --- Simulate the IBKR server side ---
    A = pow(_G, a, _P)               # client's public value (== g^a)
    b = 0x1234567890ABCDEF1234       # server's private
    B = pow(_G, b, _P)               # server's public value
    K_server = pow(A, b, _P)         # shared secret g^(ab)
    server_lst = hmac.new(_to_byte_array(K_server), raw_secret, hashlib.sha1).digest()
    server_lst_b64 = base64.b64encode(server_lst).decode()
    server_lst_sig = hmac.new(
        server_lst, auth.consumer_key.encode(), hashlib.sha1).hexdigest()

    # --- Client derives LST from the server's DH response ---
    client_lst = auth.compute_lst(format(B, "x"), a)

    assert client_lst == server_lst_b64, "client and server must derive the same LST"
    assert auth.validate_lst(server_lst_sig) is True, "LST must validate against server sig"
    assert auth.validate_lst("deadbeef") is False, "a bad signature must fail validation"
    assert auth.lst_valid is True


def test_lst_header_signature_is_percent_encoded_exactly_once():
    """The signature IBKR receives must decode with a single unquote — this is
    the wire format their server validates against (see ibind / IBKR's own
    sample: the header inserts the once-quoted signature verbatim). A doubly
    encoded signature fails live with error 23804 'Error validating signature'
    even though every local crypto test passes."""
    from urllib.parse import unquote
    from cryptography.hazmat.primitives import hashes

    auth, sig_key, _ = _make_auth()
    _url, headers, _a = auth.build_lst_request()
    header = headers["Authorization"]
    sig_field = next(
        part for part in header.split(", ") if part.startswith("oauth_signature=")
    )
    wire_sig = sig_field.split('"')[1]
    decoded_once = unquote(wire_sig)
    # A 2048-bit RSA signature is 256 bytes -> base64 always pads with '=='.
    assert decoded_once.endswith("=="), (
        "single unquote must yield raw base64 (double encoding leaves %3D)"
    )
    raw = base64.b64decode(decoded_once, validate=True)
    # Rebuild the base string exactly as signed: header params minus the sig.
    from arbiter.auth import ibkr_oauth as mod
    params = {}
    for part in header.replace('OAuth realm="limited_poa", ', "").split(", "):
        k, _, v = part.partition("=")
        if k != "oauth_signature":
            params[unquote(k)] = unquote(v.strip('"'))
    prepend = auth._decrypt_access_token_secret().hex()
    base = auth.signature_base_string("POST", _url, params, prepend=prepend)
    sig_key.public_key().verify(raw, base.encode(), padding.PKCS1v15(), hashes.SHA256())


def test_auth_header_signs_with_lst_hmac_sha256():
    auth, _, _ = _make_auth()
    auth._lst_b64 = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    auth._lst_expires = 9e18
    hdr = auth.auth_header("GET", "https://api.ibkr.com/v1/api/portfolio/accounts")
    assert hdr.startswith('OAuth realm="limited_poa"')
    assert 'oauth_signature_method="HMAC-SHA256"' in hdr
    assert 'oauth_consumer_key="ARBITER01"' in hdr
    assert "oauth_signature=" in hdr


def test_auth_header_requires_lst():
    auth, _, _ = _make_auth()
    import pytest
    with pytest.raises(RuntimeError, match="no live session token"):
        auth.auth_header("GET", "https://x")


def test_auth_header_signs_query_params_but_keeps_them_out_of_header(monkeypatch):
    """OAuth 1.0a: URL query params belong in the signature base string but
    must NOT appear in the Authorization header (they travel in the URL)."""
    auth, _, _ = _make_auth()
    auth._lst_b64 = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    auth._lst_expires = 9e18
    seen: dict = {}
    orig = auth.signature_base_string

    def spy(method, url, params, **kw):
        seen.update(params)
        return orig(method, url, params, **kw)

    monkeypatch.setattr(auth, "signature_base_string", spy)
    hdr = auth.auth_header(
        "GET", "https://api.ibkr.com/v1/api/iserver/secdef/search",
        query_params={"symbol": "FF", "conid": 42},
    )
    assert seen["symbol"] == "FF"
    assert seen["conid"] == "42"  # coerced to str for signing
    assert "symbol" not in hdr
    assert "conid" not in hdr
    assert "oauth_signature=" in hdr


def test_invalidate_drops_cached_lst():
    auth, _, _ = _make_auth()
    auth._lst_b64 = "abc"
    auth._lst_expires = 9e18
    assert auth.lst_valid is True
    auth.invalidate()
    assert auth.lst_valid is False


# ── async LST fetch (ensure_live_session_token) ─────────────────────────────

import json as _json
import re as _re


class _FakeResp:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text


class _FakeIbkrServer:
    """Plays the IBKR side of the DH handshake: reads the client's
    diffie_hellman_challenge out of the Authorization header and answers with
    a mathematically-consistent (or deliberately corrupted) response."""

    def __init__(self, auth, raw_secret: bytes, status: int = 200,
                 corrupt_sig: bool = False):
        self._auth = auth
        self._raw = raw_secret
        self._status = status
        self._corrupt = corrupt_sig
        self.post_count = 0

    def post(self, url, headers=None, **kw):
        self.post_count += 1
        if self._status != 200:
            return _FakeResp(self._status, "server error")
        m = _re.search(
            r'diffie_hellman_challenge="([0-9a-fA-F]+)"', headers["Authorization"]
        )
        A = int(m.group(1), 16)
        b = 0xB0B5EC0DE
        B = pow(_G, b, _P)
        K = pow(A, b, _P)
        lst = hmac.new(_to_byte_array(K), self._raw, hashlib.sha1).digest()
        sig = hmac.new(
            lst, self._auth.consumer_key.encode(), hashlib.sha1
        ).hexdigest()
        if self._corrupt:
            sig = "deadbeef" * 5
        return _FakeResp(200, _json.dumps({
            "diffie_hellman_response": format(B, "x"),
            "live_session_token_signature": sig,
        }))


def test_ensure_live_session_token_fetches_and_validates():
    import asyncio
    auth, _, raw = _make_auth(b"prepend-async-7")
    server = _FakeIbkrServer(auth, raw)
    lst = asyncio.run(auth.ensure_live_session_token(server))
    assert lst == auth._lst_b64
    assert auth.lst_valid is True
    assert server.post_count == 1


def test_ensure_live_session_token_caches_until_expiry():
    import asyncio
    auth, _, raw = _make_auth(b"prepend-cache-8")
    server = _FakeIbkrServer(auth, raw)

    async def twice():
        first = await auth.ensure_live_session_token(server)
        second = await auth.ensure_live_session_token(server)
        return first, second

    first, second = asyncio.run(twice())
    assert first == second
    assert server.post_count == 1, "valid cached LST must not re-handshake"


def test_ensure_live_session_token_rejects_bad_signature():
    import asyncio
    import pytest
    auth, _, raw = _make_auth(b"prepend-corrupt-9")
    server = _FakeIbkrServer(auth, raw, corrupt_sig=True)
    with pytest.raises(RuntimeError, match="signature validation"):
        asyncio.run(auth.ensure_live_session_token(server))
    # The unverified LST must NOT be left signable.
    assert auth.lst_valid is False
    with pytest.raises(RuntimeError, match="no live session token"):
        auth.auth_header("GET", "https://x")


def test_ensure_live_session_token_non_200_raises():
    import asyncio
    import pytest
    auth, _, raw = _make_auth()
    server = _FakeIbkrServer(auth, raw, status=503)
    with pytest.raises(RuntimeError, match="failed 503"):
        asyncio.run(auth.ensure_live_session_token(server))
    assert auth.lst_valid is False
