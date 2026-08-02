"""Sets/clears the httpOnly cookies that carry the JWT access/refresh tokens.

Centralized here so the cookie name/flags can't drift between the login,
refresh, and logout views - all three need to agree exactly, since browsers
treat a cookie set with a different path/samesite as a *different* cookie
(delete_cookie silently no-ops if the path doesn't match what set_cookie used).
"""
from django.conf import settings

ACCESS_COOKIE = 'access_token'
REFRESH_COOKIE = 'refresh_token'

# The refresh cookie is scoped to only the endpoints that need it, rather than
# every request like the access cookie - it's the longer-lived, more sensitive
# of the two, so there's no reason for the browser to attach it anywhere else.
_REFRESH_COOKIE_PATH = '/api/auth/'


def set_auth_cookies(response, *, access=None, refresh=None):
    if access is not None:
        response.set_cookie(
            ACCESS_COOKIE, access,
            max_age=int(settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds()),
            httponly=True, secure=settings.AUTH_COOKIE_SECURE, samesite=settings.AUTH_COOKIE_SAMESITE,
            path='/',
        )
    if refresh is not None:
        response.set_cookie(
            REFRESH_COOKIE, refresh,
            max_age=int(settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds()),
            httponly=True, secure=settings.AUTH_COOKIE_SECURE, samesite=settings.AUTH_COOKIE_SAMESITE,
            path=_REFRESH_COOKIE_PATH,
        )


def clear_auth_cookies(response):
    response.delete_cookie(ACCESS_COOKIE, path='/', samesite=settings.AUTH_COOKIE_SAMESITE)
    response.delete_cookie(REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH, samesite=settings.AUTH_COOKIE_SAMESITE)
