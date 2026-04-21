"""Ed25519 signer for Polymarket US DCM authenticated requests.

Signing payload:  {timestamp_ms}{METHOD}{path}

Body is intentionally excluded from the signing payload.
See: docs.polymarket.us/api-reference/authentication
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SignatureError(ValueError):
    """Raised when the Ed25519 secret is malformed or too short."""


@dataclass
class Ed25519Signer:
    """Signs Polymarket US API requests using Ed25519.

    Parameters
    ----------
    key_id:
        The access-key identifier sent in the ``X-PM-Access-Key`` header.
    secret_b64:
        Base64-encoded Ed25519 private key seed (must decode to >= 32 bytes;
        only the first 32 bytes are used).
    """

    key_id: str
    secret_b64: str

    def __post_init__(self) -> None:
        try:
            raw = base64.b64decode(self.secret_b64)
        except Exception as exc:
            raise SignatureError(
                "secret_b64 is not valid base64"
            ) from exc

        if len(raw) < 32:
            raise SignatureError(
                f"Ed25519 seed must be at least 32 bytes; got {len(raw)}"
            )

        # Store only the private key object — never keep raw secret bytes
        # as an attribute so they are not accidentally serialised.
        self._private_key: Ed25519PrivateKey = Ed25519PrivateKey.from_private_bytes(
            raw[:32]
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def headers(
        self,
        method: str,
        path: str,
        ts_ms: int | None = None,
        body: str | None = None,  # accepted but intentionally NOT signed
    ) -> dict[str, str]:
        """Return the three Polymarket US authentication headers.

        Parameters
        ----------
        method:
            HTTP verb (e.g. ``"GET"``, ``"POST"``).
        path:
            Request path including query string (e.g. ``"/v1/markets"``).
        ts_ms:
            Unix timestamp in milliseconds. Defaults to :meth:`now_ms`.
        body:
            Request body. Accepted for API compatibility but intentionally
            NOT included in the signing payload per
            docs.polymarket.us/api-reference/authentication.
        """
        timestamp = ts_ms if ts_ms is not None else self.now_ms()

        # body is NOT included in the payload — this is the spec.
        payload = f"{timestamp}{method}{path}".encode()
        signature_bytes = self._private_key.sign(payload)
        signature_b64 = base64.b64encode(signature_bytes).decode()

        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": str(timestamp),
            "X-PM-Signature": signature_b64,
        }

    @staticmethod
    def now_ms() -> int:
        """Return the current UTC time as milliseconds since the epoch."""
        return int(time.time() * 1000)
