"""HMAC-SHA256 webhook signature verification.

The sender signs ``f"{timestamp}.{raw_body}"`` with the shared secret and sends:

    X-Timestamp: 1767225600
    X-Signature: sha256=<hex digest>

Including the timestamp in the signed string, and rejecting stale timestamps,
stops an attacker from replaying a captured request later.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
_PREFIX = "sha256="


class SignatureError(Exception):
    """The request is not signed correctly. Mapped to HTTP 401."""


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> int:
    """Return the verified timestamp (unix seconds) or raise SignatureError."""
    if not timestamp or not signature:
        raise SignatureError("missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("timestamp is not an integer") from exc

    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance window")

    expected = compute_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature.strip()):
        raise SignatureError("signature mismatch")
    return ts
