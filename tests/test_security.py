import pytest

from app.security import SignatureError, compute_signature, verify_signature

SECRET = "test-secret"
BODY = b'{"hello": "world"}'
NOW = 1_767_225_600


def test_valid_signature_returns_timestamp():
    sig = compute_signature(SECRET, str(NOW), BODY)
    assert verify_signature(SECRET, str(NOW), sig, BODY, now=NOW) == NOW


@pytest.mark.parametrize(
    ("timestamp", "signature", "body"),
    [
        (None, "sha256=abc", BODY),  # missing timestamp
        (str(NOW), None, BODY),  # missing signature
        ("not-a-number", "sha256=abc", BODY),
        (str(NOW), "sha256=" + "0" * 64, BODY),  # wrong digest
        (str(NOW), compute_signature("other-secret", str(NOW), BODY), BODY),
        (str(NOW), compute_signature(SECRET, str(NOW), BODY), BODY + b" "),  # tampered
    ],
)
def test_invalid_signatures_rejected(timestamp, signature, body):
    with pytest.raises(SignatureError):
        verify_signature(SECRET, timestamp, signature, body, now=NOW)


def test_stale_timestamp_rejected_even_with_valid_signature():
    old = str(NOW - 301)
    sig = compute_signature(SECRET, old, BODY)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_signature(SECRET, old, sig, BODY, now=NOW, tolerance_seconds=300)
