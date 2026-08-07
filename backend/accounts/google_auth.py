"""Server-side verification of a Google OAuth access token.

The frontend uses the classic OAuth popup flow (@react-oauth/google's
useGoogleLogin, implicit flow) rather than Google Identity Services' rendered
button, specifically because GIS's button silently swaps to a "Sign in as
<name>" personalized/auto-select experience whenever the browser already has
a Google session (FedCM) - the popup flow always shows Google's full account
chooser instead, matching the classic "Sign in with Google" UX. That flow
hands the frontend an access token, not an ID token, so verification here
looks different from a typical ID-token-JWT check: two plain REST calls to
Google - tokeninfo (to confirm the token was actually issued to *this* app,
the equivalent of an ID token's `aud` check) and userinfo (for the actual
profile claims) - rather than local JWT signature verification.

Kept separate from serializers.py so tests can @patch verify_google_access_token
directly instead of stubbing out the two network calls individually.
"""
import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

GOOGLE_TOKENINFO_URL = 'https://oauth2.googleapis.com/tokeninfo'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
_REQUEST_TIMEOUT_SECONDS = 10


class GoogleTokenError(Exception):
    """The access token failed verification (invalid/expired, or not issued
    to this app's client ID)."""


def verify_google_access_token(access_token):
    """Confirms `access_token` is valid and was issued to GOOGLE_CLIENT_ID,
    then fetches the associated profile.

    Returns a claims dict shaped like an ID token's (sub, email,
    email_verified, given_name, family_name) so callers (GoogleLoginSerializer)
    don't need to know which flow produced it. Raises GoogleTokenError on any
    failure - invalid/expired token, wrong audience, or Google being
    unreachable.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise ImproperlyConfigured(
            'GOOGLE_CLIENT_ID is not configured - create an OAuth client at '
            'https://console.cloud.google.com/apis/credentials and set it in .env.'
        )

    try:
        tokeninfo_response = requests.get(
            GOOGLE_TOKENINFO_URL, params={'access_token': access_token}, timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise GoogleTokenError(f'Could not verify Google access token: {exc}') from exc
    if not tokeninfo_response.ok:
        raise GoogleTokenError('Invalid or expired Google access token.')

    tokeninfo = tokeninfo_response.json()
    # The audience check an ID token's signature verification would normally
    # give us for free - without it, any access token the client happens to
    # hold (from a *different* Google OAuth app) could be replayed against
    # this endpoint directly (it's a plain POST body, not tied to our frontend).
    if tokeninfo.get('aud') != settings.GOOGLE_CLIENT_ID:
        raise GoogleTokenError('Access token was not issued for this application.')

    try:
        userinfo_response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise GoogleTokenError(f'Could not fetch Google profile: {exc}') from exc
    if not userinfo_response.ok:
        raise GoogleTokenError('Could not fetch Google profile.')

    userinfo = userinfo_response.json()
    return {
        'sub': userinfo.get('sub') or tokeninfo.get('sub'),
        'email': userinfo.get('email'),
        'email_verified': bool(userinfo.get('email_verified')),
        'given_name': userinfo.get('given_name', ''),
        'family_name': userinfo.get('family_name', ''),
    }
