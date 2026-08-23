"""Named per-endpoint throttles, on top of the blanket Anon/User throttles in
REST_FRAMEWORK. Each maps to a 'scope' -> rate entry in DEFAULT_THROTTLE_RATES
(settings.py), so the rate lives in one place instead of a raw string on every view.

Built on SimpleRateThrottle (like DRF's own AnonRateThrottle/UserRateThrottle)
rather than ScopedRateThrottle: ScopedRateThrottle reads its scope from a
`throttle_scope` attribute on the *view*, not from the throttle class, which is
easy to forget to set and then silently never throttles anything.
"""
from rest_framework.throttling import SimpleRateThrottle


class ScopedIdentityRateThrottle(SimpleRateThrottle):
    """Keyed by whichever identity is available - user id when authenticated,
    IP otherwise - so the same base works for both anonymous endpoints (login,
    register) and authenticated ones (AI, analysis creation)."""

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class LoginRateThrottle(ScopedIdentityRateThrottle):
    scope = 'login'


class RegisterRateThrottle(ScopedIdentityRateThrottle):
    scope = 'register'


class PasswordResetRateThrottle(ScopedIdentityRateThrottle):
    scope = 'password_reset'


class OtpVerifyRateThrottle(ScopedIdentityRateThrottle):
    """IP-level backstop on the OTP-check endpoint - the real brute-force
    defense is the per-account expiry + attempt-lockout in accounts/otp.py,
    this just stops one IP hammering across many different target emails."""

    scope = 'otp_verify'


class OtpResendRateThrottle(ScopedIdentityRateThrottle):
    """Tighter than otp_verify - each resend costs a real Brevo send."""

    scope = 'otp_resend'


class AIRateThrottle(ScopedIdentityRateThrottle):
    scope = 'ai'


class AnalysisCreateRateThrottle(ScopedIdentityRateThrottle):
    scope = 'analysis_create'
