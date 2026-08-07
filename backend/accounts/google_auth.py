"""Server-side verification of Google Identity Services ID tokens.

Kept separate from serializers.py so tests can @patch verify_google_id_token
directly instead of stubbing out network calls to Google's certs endpoint.
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token


class GoogleTokenError(Exception):
    """The credential failed verification (bad signature, wrong audience, expired, etc.)."""


def verify_google_id_token(credential):
    """Verifies signature/issuer/expiry and that `aud` matches GOOGLE_CLIENT_ID.

    Returns the decoded claims dict (sub, email, email_verified, given_name,
    family_name, ...) on success; raises GoogleTokenError otherwise.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise ImproperlyConfigured(
            'GOOGLE_CLIENT_ID is not configured - create an OAuth client at '
            'https://console.cloud.google.com/apis/credentials and set it in .env.'
        )
    try:
        return id_token.verify_oauth2_token(credential, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
    except ValueError as exc:
        raise GoogleTokenError(str(exc)) from exc
