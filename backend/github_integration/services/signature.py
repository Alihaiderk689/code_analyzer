"""Verifies the X-Hub-Signature-256 header GitHub sends on every webhook
delivery, proving the request actually came from GitHub (or at least from
someone who knows the shared webhook secret) and wasn't forged/tampered with
in transit. Uses hmac.compare_digest specifically to avoid a timing attack
that a naive `==` string comparison would be vulnerable to.
"""
import hmac
import hashlib


def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header or not secret:
        return False
    if not signature_header.startswith('sha256='):
        return False

    expected = 'sha256=' + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
