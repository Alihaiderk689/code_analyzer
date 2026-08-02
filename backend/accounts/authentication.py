"""Cookie-aware JWT authentication.

Reads the access token from an `Authorization: Bearer ...` header if present
(non-browser clients - scripts, admin tooling), otherwise falls back to the
httpOnly access_token cookie the frontend now relies on exclusively.

Cookie-sourced auth additionally enforces CSRF (double-submit cookie) on unsafe
methods: unlike a bearer header, a cookie is attached to requests automatically
by the browser, so without this any other site could trigger authenticated
requests just by getting a logged-in user to visit it. Header-sourced auth
doesn't need this - no other site can read/set an Authorization header for
this origin.
"""
from rest_framework import exceptions
from rest_framework.authentication import CSRFCheck
from rest_framework_simplejwt.authentication import JWTAuthentication

from .cookies import ACCESS_COOKIE


class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        header = self.get_header(request)
        if header is not None:
            raw_token = self.get_raw_token(header)
            if raw_token is not None:
                validated_token = self.get_validated_token(raw_token)
                return self.get_user(validated_token), validated_token

        raw_token = request.COOKIES.get(ACCESS_COOKIE)
        if raw_token is None:
            return None

        validated_token = self.get_validated_token(raw_token)
        self._enforce_csrf(request)
        return self.get_user(validated_token), validated_token

    def _enforce_csrf(self, request):
        check = CSRFCheck(lambda r: None)
        # Populates request.META['CSRF_COOKIE'], read by process_view() below.
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            raise exceptions.PermissionDenied(f'CSRF Failed: {reason}')
