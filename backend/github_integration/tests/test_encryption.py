from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from ..services.encryption import TokenDecryptionError, decrypt_token, encrypt_token
from .factories import TEST_ENCRYPTION_KEY


@override_settings(GITHUB_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class EncryptionRoundTripTests(TestCase):
    def test_round_trip_returns_original_token(self):
        encrypted = encrypt_token('gho_supersecrettoken')
        self.assertEqual(decrypt_token(encrypted), 'gho_supersecrettoken')

    def test_ciphertext_does_not_contain_plaintext(self):
        encrypted = encrypt_token('gho_supersecrettoken')
        self.assertNotIn(b'gho_supersecrettoken', encrypted)

    def test_encrypting_the_same_token_twice_yields_different_ciphertext(self):
        # Fernet includes a random IV/nonce per encryption - two encryptions of
        # the same token must not be byte-identical (would leak equality).
        first = encrypt_token('same-token')
        second = encrypt_token('same-token')
        self.assertNotEqual(first, second)

    def test_tampered_ciphertext_raises_decryption_error(self):
        encrypted = bytearray(encrypt_token('gho_supersecrettoken'))
        encrypted[-1] ^= 0xFF  # flip the last byte
        with self.assertRaises(TokenDecryptionError):
            decrypt_token(bytes(encrypted))

    def test_decrypting_with_a_different_key_raises_decryption_error(self):
        encrypted = encrypt_token('gho_supersecrettoken')
        with override_settings(GITHUB_TOKEN_ENCRYPTION_KEY='6q6hTxEo7mioqPZSAmtLRpjDOfLxNH_FBUim-i6u-Q8='):
            with self.assertRaises(TokenDecryptionError):
                decrypt_token(encrypted)


class EncryptionNotConfiguredTests(TestCase):
    @override_settings(GITHUB_TOKEN_ENCRYPTION_KEY='')
    def test_encrypt_raises_improperly_configured_when_key_missing(self):
        with self.assertRaises(ImproperlyConfigured):
            encrypt_token('token')

    @override_settings(GITHUB_TOKEN_ENCRYPTION_KEY='')
    def test_decrypt_raises_improperly_configured_when_key_missing(self):
        with self.assertRaises(ImproperlyConfigured):
            decrypt_token(b'anything')
