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

# Binds the GitHub login flow's signed `state` nonce to the browser that
# started it (github_integration's GitHubLoginInitiateView sets this,
# GitHubCallbackView verifies the callback's state nonce matches it) - without
# this, `state`'s signature alone only proves *we* issued it, not that the
# browser completing the callback is the one that started the flow, which is
# exactly the OAuth login-CSRF class of bug (an attacker can complete their
# own authorize step, then hand the resulting code+state to a victim to
# silently log the victim into the attacker's account). Explicitly SameSite=
# Lax (independent of AUTH_COOKIE_SAMESITE, which is None in production for
# the JWT cookies) since it must survive exactly one thing: the top-level GET
# redirect back from github.com, which Lax already allows.
GITHUB_LOGIN_NONCE_COOKIE = 'github_login_nonce'
_GITHUB_LOGIN_NONCE_COOKIE_PATH = '/api/'
_GITHUB_LOGIN_NONCE_MAX_AGE_SECONDS = 600  # matches oauth_service._STATE_MAX_AGE_SECONDS


def set_github_login_nonce_cookie(response, nonce):
    response.set_cookie(
        GITHUB_LOGIN_NONCE_COOKIE, nonce,
        max_age=_GITHUB_LOGIN_NONCE_MAX_AGE_SECONDS,
        httponly=True, secure=settings.AUTH_COOKIE_SECURE, samesite='Lax',
        path=_GITHUB_LOGIN_NONCE_COOKIE_PATH,
    )


def clear_github_login_nonce_cookie(response):
    response.delete_cookie(GITHUB_LOGIN_NONCE_COOKIE, path=_GITHUB_LOGIN_NONCE_COOKIE_PATH, samesite='Lax')


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
