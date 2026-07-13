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
