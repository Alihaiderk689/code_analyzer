"""Encrypts GitHub access tokens at rest using Fernet (symmetric, from the
`cryptography` package already used elsewhere in this project for PDF
signing) - a stolen database backup should not hand out live GitHub tokens.

Validated lazily, on first actual use, not at import/Django-startup time -
same pattern as GROQ_API_KEY in ai/client.py: the rest of the app must still
boot and run when GitHub integration hasn't been configured yet.

Single-key by design, for now. Rotating GITHUB_TOKEN_ENCRYPTION_KEY today
makes every stored token undecryptable (TokenDecryptionError below), which
degrades to "affected users reconnect GitHub" rather than data loss - the
tokens are re-obtainable through the OAuth flow at any time, and nothing else
is encrypted with this key. Supporting rotation without that reconnect would
mean MultiFernet (decrypt against a list of keys, encrypt with the first) plus
a re-encryption management command; that is deliberately not built yet, since
it adds a key-list configuration surface to solve a problem this deployment
does not currently have. Tracked as a backlog item in docs/SECURITY.md - revisit if
tokens ever become non-reissuable, if a compliance requirement mandates
scheduled key rotation, or if the user base makes mass reconnection costly.
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TokenDecryptionError(Exception):
    """Raised when stored ciphertext can't be decrypted with the configured
    key - e.g. GITHUB_TOKEN_ENCRYPTION_KEY was rotated without re-encrypting
    existing rows. Callers should treat this the same as an invalid token."""


def _get_fernet() -> Fernet:
    key = settings.GITHUB_TOKEN_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(
            'GITHUB_TOKEN_ENCRYPTION_KEY is not configured. Generate one with: '
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(raw_token: str) -> bytes:
    return _get_fernet().encrypt(raw_token.encode())


def decrypt_token(encrypted_token: bytes) -> str:
    try:
        return _get_fernet().decrypt(encrypted_token).decode()
    except InvalidToken as exc:
        raise TokenDecryptionError('Stored GitHub access token could not be decrypted.') from exc
