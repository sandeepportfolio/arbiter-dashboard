"""Tests for Ed25519Signer — Polymarket US DCM authentication.

These tests MUST be written before the implementation (TDD). They pin the
critical invariant that body is NOT included in the signing payload.
"""

import base64
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from arbiter.auth.ed25519_signer import Ed25519Signer, SignatureError

# 32-byte test secret: bytes 0x00..0x1F, base64-encoded.
# DO NOT use this in production.
_TEST_SECRET_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
_TEST_KEY_ID = "test-key-id"


# ---------------------------------------------------------------------------
# test_headers_shape
# ---------------------------------------------------------------------------

def test_headers_shape():
    """Returned dict must have all 3 expected headers with correct types."""
    signer = Ed25519Signer(key_id=_TEST_KEY_ID, secret_b64=_TEST_SECRET_B64)
    headers = signer.headers(method="GET", path="/v1/markets")

    assert isinstance(headers, dict), "headers() must return a dict"

    assert "X-PM-Access-Key" in headers, "Missing X-PM-Access-Key"
    assert "X-PM-Timestamp" in headers, "Missing X-PM-Timestamp"
    assert "X-PM-Signature" in headers, "Missing X-PM-Signature"

    assert isinstance(headers["X-PM-Access-Key"], str)
    assert isinstance(headers["X-PM-Timestamp"], str)
    assert isinstance(headers["X-PM-Signature"], str)

    # Timestamp must be a numeric string (milliseconds)
    assert headers["X-PM-Timestamp"].isdigit(), (
        "X-PM-Timestamp must be a numeric string"
    )

    # Access key must equal the key_id passed in
    assert headers["X-PM-Access-Key"] == _TEST_KEY_ID

    # Signature must be valid base64
    decoded = base64.b64decode(headers["X-PM-Signature"])
    assert len(decoded) == 64, "Ed25519 signature must be 64 bytes"


# ---------------------------------------------------------------------------
# test_signature_payload_excludes_body
# ---------------------------------------------------------------------------

def test_signature_payload_excludes_body():
    """Critical regression guard: body is NOT part of the signing payload.

    Same timestamp + method + path but different bodies MUST produce
    identical signatures. This matches docs.polymarket.us/api-reference/authentication.
    """
    signer = Ed25519Signer(key_id=_TEST_KEY_ID, secret_b64=_TEST_SECRET_B64)
    fixed_ts = 1_700_000_000_000  # fixed ms timestamp

    headers_no_body = signer.headers(
        method="POST",
        path="/v1/order",
        ts_ms=fixed_ts,
        body=None,
    )
    headers_with_body = signer.headers(
        method="POST",
        path="/v1/order",
        ts_ms=fixed_ts,
        body='{"price": "0.55", "size": "100"}',
    )
    headers_different_body = signer.headers(
        method="POST",
        path="/v1/order",
        ts_ms=fixed_ts,
        body='{"price": "0.99", "size": "1"}',
    )

    sig_no_body = headers_no_body["X-PM-Signature"]
    sig_with_body = headers_with_body["X-PM-Signature"]
    sig_different_body = headers_different_body["X-PM-Signature"]

    assert sig_no_body == sig_with_body, (
        "Signature changed when body was added — body must NOT be in the payload"
    )
    assert sig_with_body == sig_different_body, (
        "Signature changed with different body content — body must NOT be in the payload"
    )


# ---------------------------------------------------------------------------
# test_wrong_secret_length_raises
# ---------------------------------------------------------------------------

def test_wrong_secret_length_raises():
    """A secret that decodes to fewer than 32 bytes must raise SignatureError."""
    # 10 bytes encoded — too short for Ed25519
    short_secret = base64.b64encode(b"\x00" * 10).decode()
    with pytest.raises(SignatureError):
        Ed25519Signer(key_id=_TEST_KEY_ID, secret_b64=short_secret)


def test_invalid_base64_raises():
    """A non-base64 string must raise SignatureError."""
    with pytest.raises(SignatureError):
        Ed25519Signer(key_id=_TEST_KEY_ID, secret_b64="not-valid-base64!!!")


# ---------------------------------------------------------------------------
# test_signature_deterministic_and_verifiable
# ---------------------------------------------------------------------------

def test_signature_deterministic_and_verifiable():
    """Signature verifies against the derived public key for the expected message.

    Message bytes are f"{ts}{METHOD}{path}".encode() — no body.
    """
    signer = Ed25519Signer(key_id=_TEST_KEY_ID, secret_b64=_TEST_SECRET_B64)
    ts = 1_700_000_000_000
    method = "GET"
    path = "/v1/positions"

    headers = signer.headers(method=method, path=path, ts_ms=ts)

    # Reconstruct the expected message
    expected_message = f"{ts}{method}{path}".encode()

    # Decode the signature from the header
    sig_bytes = base64.b64decode(headers["X-PM-Signature"])

    # Derive the public key from the same secret and verify
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64 as _b64

    raw_secret = _b64.b64decode(_TEST_SECRET_B64)[:32]
    private_key = Ed25519PrivateKey.from_private_bytes(raw_secret)
    public_key = private_key.public_key()

    # cryptography's verify() raises InvalidSignature on failure
    public_key.verify(sig_bytes, expected_message)  # must not raise


def test_signature_is_deterministic():
    """Same inputs produce the same signature (Ed25519 is deterministic)."""
    signer = Ed25519Signer(key_id=_TEST_KEY_ID, secret_b64=_TEST_SECRET_B64)
    ts = 1_700_000_000_001
    kwargs = dict(method="DELETE", path="/v1/order/123", ts_ms=ts)

    h1 = signer.headers(**kwargs)
    h2 = signer.headers(**kwargs)

    assert h1["X-PM-Signature"] == h2["X-PM-Signature"], (
        "Ed25519 signatures must be deterministic for the same inputs"
    )
