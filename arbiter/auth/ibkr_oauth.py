"""IBKR Web API OAuth 1.0a (first-party / self-service) authentication.

The zero-touch, machine-to-machine "API keys" path for ForecastEx: no local
gateway, no browser login, no 2FA tap. The bot derives a ~24h Live Session
Token (LST) from a keypair handshake, then signs every request with it.

Flow (per IBKR OAuth 1.0a spec):
  1. Decrypt the registration access-token secret with the RSA *encryption*
     private key -> the "prepend" used in the LST signature base string.
  2. Diffie-Hellman challenge: A = g^a mod p (random a).
  3. POST /oauth/live_session_token, signed RSA-SHA256 with the *signature*
     private key. Response gives B = g^b mod p and an LST signature.
  4. Shared secret K = B^a mod p. LST = base64(HMAC-SHA1(K_bytes, prepend_bytes)).
  5. Validate LST against the returned signature (HMAC-SHA1(LST, consumer_key)).
  6. Sign each API request OAuth-1.0a HMAC-SHA256 with key = base64decode(LST).

⚠️ LIVE-VALIDATION PENDING: the RSA-SHA256 signing, HMAC signing, signature
base-string construction and header formatting are unit-tested here, but the
end-to-end DH handshake can only be validated against IBKR once the operator's
consumer key is activated (~1-2 weeks after registration). Do NOT flip the
collector to OAuth mode in production until get_live_session_token() succeeds
against api.ibkr.com and validate_lst() returns True. See
deploy/ib-gateway/RUNBOOK.md § Path B.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# RFC 3986 unreserved set — everything else is percent-encoded in OAuth.
def _rfc3986(value: str) -> str:
    return quote(str(value), safe="~-._")


def _nonce() -> str:
    # 16 random hex chars. random (not secrets) is acceptable for an OAuth
    # nonce (uniqueness, not secrecy) and keeps this import-light.
    return "".join(random.choices("0123456789abcdef", k=16))


def _to_byte_array(n: int) -> bytes:
    """Big-endian bytes of a positive int, with a leading 0x00 when the high
    bit is set — the exact convention IBKR uses for the DH shared secret so
    the HMAC key matches their server-side derivation."""
    length = (n.bit_length() + 7) // 8
    b = n.to_bytes(length, "big")
    if b and (b[0] & 0x80):
        b = b"\x00" + b
    return b


@dataclass
class IbkrOAuth1a:
    """Holds OAuth 1.0a credentials + a cached Live Session Token.

    Construct via ``from_config`` (reads key files from disk once)."""

    consumer_key: str
    access_token: str
    access_token_secret: str          # base64, RSA-encrypted (as issued)
    signature_key_pem: bytes          # private RSA signing key
    encryption_key_pem: bytes         # private RSA encryption key
    dh_prime: int                     # DH prime p
    dh_generator: int                 # DH generator g
    realm: str = "limited_poa"
    api_base: str = "https://api.ibkr.com/v1/api"

    _lst_b64: Optional[str] = None
    _lst_expires: float = 0.0

    # ── construction ────────────────────────────────────────────────
    @classmethod
    def from_config(cls, cfg) -> "IbkrOAuth1a":
        """Build from a ForecastExConfig (reads the three key files)."""
        with open(cfg.oauth_signature_key_fp, "rb") as fh:
            sig = fh.read()
        with open(cfg.oauth_encryption_key_fp, "rb") as fh:
            enc = fh.read()
        prime, gen = cls._read_dh_params(cfg.oauth_dh_param_fp)
        return cls(
            consumer_key=cfg.oauth_consumer_key,
            access_token=cfg.oauth_access_token,
            access_token_secret=cfg.oauth_access_token_secret,
            signature_key_pem=sig,
            encryption_key_pem=enc,
            dh_prime=prime,
            dh_generator=gen,
            realm=cfg.oauth_realm,
            api_base=cfg.oauth_api_base,
        )

    @staticmethod
    def _read_dh_params(path: str) -> tuple[int, int]:
        # cryptography >= 49 rejects this PKCS#3 file ("Invalid DH
        # parameters") that release 46 accepted — and the file is the one
        # registered with IBKR, so it can never be regenerated to appease
        # the library. Only the integers p and g are needed here; decode
        # the ASN.1 SEQUENCE ourselves so a dependency bump can never take
        # the whole API down at boot again (crash loop seen 2026-07-29).
        with open(path, "rb") as fh:
            pem = fh.read()
        m = re.search(
            rb"-----BEGIN DH PARAMETERS-----(.+?)-----END DH PARAMETERS-----",
            pem, re.S,
        )
        if not m:  # X9.42 or other container — fall back to the library.
            params = serialization.load_pem_parameters(pem)
            nums = params.parameter_numbers()
            return nums.p, nums.g
        der = base64.b64decode(b"".join(m.group(1).split()))

        def _read_len(i: int) -> tuple[int, int]:
            n = der[i]
            i += 1
            if n & 0x80:
                k = n & 0x7F
                n = int.from_bytes(der[i:i + k], "big")
                i += k
            return n, i

        if der[0] != 0x30:
            raise ValueError(f"{path}: expected DER SEQUENCE in DH PARAMETERS")
        _, i = _read_len(1)
        ints = []
        for _ in range(2):  # DHParameter ::= SEQUENCE { prime, base, ... }
            if der[i] != 0x02:
                raise ValueError(f"{path}: expected DER INTEGER in DH PARAMETERS")
            length, i = _read_len(i + 1)
            ints.append(int.from_bytes(der[i:i + length], "big"))
            i += length
        return ints[0], ints[1]

    # ── testable primitives ─────────────────────────────────────────
    @staticmethod
    def signature_base_string(method: str, url: str, params: dict, prepend: str = "") -> str:
        """OAuth 1.0a signature base string: [prepend]METHOD&url&sorted-params.

        ``prepend`` is empty for HMAC request signing and the decrypted
        access-token secret (hex) for the LST request."""
        norm = "&".join(
            f"{_rfc3986(k)}={_rfc3986(v)}" for k, v in sorted(params.items())
        )
        return f"{prepend}{method.upper()}&{_rfc3986(url)}&{_rfc3986(norm)}"

    def _rsa_sha256_sign(self, base_string: str) -> str:
        # Returns RAW base64 — _oauth_header percent-encodes header values
        # exactly once; encoding here too double-encodes the signature, which
        # IBKR rejects live as error 23804 "Error validating signature".
        key = serialization.load_pem_private_key(self.signature_key_pem, password=None)
        sig = key.sign(base_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode("utf-8")

    @staticmethod
    def _hmac_sha256_sign(base_string: str, lst_b64: str) -> str:
        # Raw base64, same single-encoding contract as _rsa_sha256_sign.
        key = base64.b64decode(lst_b64)
        digest = hmac.new(key, base_string.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    @staticmethod
    def _oauth_header(realm: str, params: dict) -> str:
        inner = ", ".join(
            f'{_rfc3986(k)}="{_rfc3986(v)}"' for k, v in sorted(params.items())
        )
        return f'OAuth realm="{realm}", {inner}'

    # ── live session token (DH handshake — live-validation pending) ──
    def _decrypt_access_token_secret(self) -> bytes:
        key = serialization.load_pem_private_key(self.encryption_key_pem, password=None)
        return key.decrypt(base64.b64decode(self.access_token_secret), padding.PKCS1v15())

    def build_lst_request(self) -> tuple[str, dict, int]:
        """Assemble the signed /oauth/live_session_token request WITHOUT sending
        (so it is unit-testable). Returns (url, headers, dh_random_a)."""
        prepend = self._decrypt_access_token_secret().hex()
        a = random.getrandbits(256)
        A = pow(self.dh_generator, a, self.dh_prime)
        url = f"{self.api_base}/oauth/live_session_token"
        params = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": _nonce(),
            "oauth_signature_method": "RSA-SHA256",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.access_token,
            "diffie_hellman_challenge": format(A, "x"),
        }
        base = self.signature_base_string("POST", url, params, prepend=prepend)
        params["oauth_signature"] = self._rsa_sha256_sign(base)
        headers = {"Authorization": self._oauth_header(self.realm, params),
                   "User-Agent": "arbiter-ibkr-oauth/1.0"}
        return url, headers, a

    def compute_lst(self, dh_response_hex: str, dh_random_a: int) -> str:
        """K = B^a mod p; LST = base64(HMAC-SHA1(K_bytes, prepend_bytes))."""
        B = int(dh_response_hex, 16)
        K = pow(B, dh_random_a, self.dh_prime)
        prepend = self._decrypt_access_token_secret()
        lst = hmac.new(_to_byte_array(K), prepend, hashlib.sha1).digest()
        self._lst_b64 = base64.b64encode(lst).decode("utf-8")
        # IBKR LSTs live ~24h; refresh with a safety margin.
        self._lst_expires = time.time() + 22 * 3600
        return self._lst_b64

    def validate_lst(self, lst_signature_hex: str) -> bool:
        """LST is valid iff HMAC-SHA1(base64decode(LST), consumer_key) matches."""
        if not self._lst_b64:
            return False
        expected = hmac.new(
            base64.b64decode(self._lst_b64), self.consumer_key.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        return hmac.compare_digest(expected, lst_signature_hex)

    # ── per-request signing ─────────────────────────────────────────
    def auth_header(
        self,
        method: str,
        url: str,
        extra_oauth: Optional[dict] = None,
        query_params: Optional[dict] = None,
    ) -> str:
        """Build the OAuth 1.0a Authorization header for a live request.
        Requires a valid cached LST (compute_lst must have run).

        ``query_params`` are the request's URL query parameters: OAuth 1.0a
        requires them in the signature base string, but they must NOT appear
        in the Authorization header itself (they travel in the URL).
        """
        if not self._lst_b64:
            raise RuntimeError("no live session token — call get_live_session_token first")
        params = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": _nonce(),
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.access_token,
        }
        if extra_oauth:
            params.update(extra_oauth)
        base_params = dict(params)
        if query_params:
            base_params.update({k: str(v) for k, v in query_params.items()})
        base = self.signature_base_string(method, url, base_params)
        params["oauth_signature"] = self._hmac_sha256_sign(base, self._lst_b64)
        return self._oauth_header(self.realm, params)

    @property
    def lst_valid(self) -> bool:
        return bool(self._lst_b64) and time.time() < self._lst_expires

    def invalidate(self) -> None:
        """Drop the cached LST (e.g. after a 401) so the next
        ensure_live_session_token performs a fresh handshake."""
        self._lst_b64 = None
        self._lst_expires = 0.0

    async def ensure_live_session_token(self, session) -> str:
        """Return a valid Live Session Token, performing the DH handshake
        against IBKR when the cached one is missing/expired.

        ``session`` is an ``aiohttp.ClientSession``. Raises ``RuntimeError``
        when the request fails or the server's LST signature does not verify
        (a non-verifying LST means the handshake was corrupted or the key
        material is wrong — never sign requests with it).
        """
        if self.lst_valid:
            return self._lst_b64  # type: ignore[return-value]
        url, headers, a = self.build_lst_request()
        async with session.post(url, headers=headers) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"IBKR live_session_token request failed "
                    f"{resp.status}: {text[:200]}"
                )
        try:
            payload = json.loads(text)
            dh_response = payload["diffie_hellman_response"]
            lst_signature = payload["live_session_token_signature"]
        except (ValueError, KeyError) as exc:
            raise RuntimeError(
                f"IBKR live_session_token response malformed: {text[:200]}"
            ) from exc
        lst = self.compute_lst(dh_response, a)
        if not self.validate_lst(lst_signature):
            self.invalidate()
            raise RuntimeError(
                "IBKR live session token failed signature validation — "
                "refusing to sign requests with an unverified LST"
            )
        return lst
