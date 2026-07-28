import hashlib
import hmac

from django.test import SimpleTestCase

from ..services.signature import verify_signature


def _sign(secret: str, body: bytes) -> str:
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class VerifySignatureTests(SimpleTestCase):
    def test_valid_signature_passes(self):
        body = b'{"action": "opened"}'
        self.assertTrue(verify_signature(body, _sign('my-secret', body), 'my-secret'))

    def test_wrong_secret_fails(self):
        body = b'{"action": "opened"}'
        self.assertFalse(verify_signature(body, _sign('wrong-secret', body), 'my-secret'))

    def test_tampered_body_fails(self):
        body = b'{"action": "opened"}'
        signature = _sign('my-secret', body)
        self.assertFalse(verify_signature(b'{"action": "closed"}', signature, 'my-secret'))

    def test_missing_signature_header_fails(self):
        self.assertFalse(verify_signature(b'{}', '', 'my-secret'))

    def test_missing_secret_fails(self):
        body = b'{}'
        self.assertFalse(verify_signature(body, _sign('anything', body), ''))

    def test_signature_without_sha256_prefix_fails(self):
        body = b'{}'
        raw_hex = hmac.new(b'my-secret', body, hashlib.sha256).hexdigest()
        self.assertFalse(verify_signature(body, raw_hex, 'my-secret'))

    def test_sha1_style_header_is_rejected(self):
        body = b'{}'
        sha1_signature = 'sha1=' + hmac.new(b'my-secret', body, hashlib.sha1).hexdigest()
        self.assertFalse(verify_signature(body, sha1_signature, 'my-secret'))
