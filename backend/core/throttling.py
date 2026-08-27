"""Named per-endpoint throttles, on top of the blanket Anon/User throttles in
REST_FRAMEWORK. Each maps to a 'scope' -> rate entry in DEFAULT_THROTTLE_RATES
(settings.py), so the rate lives in one place instead of a raw string on every view.

Built on SimpleRateThrottle (like DRF's own AnonRateThrottle/UserRateThrottle)
rather than ScopedRateThrottle: ScopedRateThrottle reads its scope from a
`throttle_scope` attribute on the *view*, not from the throttle class, which is
easy to forget to set and then silently never throttles anything.
"""
import hashlib

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


class OtpVerifyAccountRateThrottle(SimpleRateThrottle):
    """Second, tighter bucket on the OTP-check endpoint, keyed by the caller's
    IP *and* the email being verified rather than the IP alone.

    What it adds over OtpVerifyRateThrottle: that one lets a single IP spend
    its entire hourly allowance guessing at one target account. This caps what
    any one (IP, target) pair can spend, so an IP with a budget for 20 checks
    an hour can no longer aim all 20 at one victim's code.

    It does not replace the IP throttle, and both are applied - the IP bucket
    still bounds an IP's total volume across every account it touches, which
    is the thing a per-target key by itself would not bound at all.

    Requests with no usable email in the body return None (no throttling from
    this class): there is nothing to key on, the request is going to fail
    serializer validation anyway, and OtpVerifyRateThrottle still covers it.
    """

    scope = 'otp_verify_account'

    def get_cache_key(self, request, view):
        data = request.data if isinstance(request.data, dict) else {}
        email = data.get('email')
        if not isinstance(email, str) or not email.strip():
            return None
        # Hashed, not raw: the cache is shared infrastructure (Redis in
        # deployment), and a key namespace enumerable into a list of the email
        # addresses that recently attempted verification is an avoidable leak.
        # Lowercased first so it matches the normalize_email() the serializer
        # applies - otherwise "A@x.com" and "a@x.com" get separate buckets.
        digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]
        return self.cache_format % {'scope': self.scope, 'ident': f'{self.get_ident(request)}:{digest}'}


class OtpResendRateThrottle(ScopedIdentityRateThrottle):
    """Tighter than otp_verify - each resend costs a real Brevo send."""

    scope = 'otp_resend'


class AIRateThrottle(ScopedIdentityRateThrottle):
    scope = 'ai'


class AnalysisCreateRateThrottle(ScopedIdentityRateThrottle):
    scope = 'analysis_create'
