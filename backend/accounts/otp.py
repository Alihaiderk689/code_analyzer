"""Registration OTP: generation, storage, and verification.

Kept separate from serializers.py/views.py so the hashing/expiry/attempt-
lockout logic has one home, same reasoning as google_auth.py/github_auth.py
being split out from the serializers that call them.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.utils import timezone

OTP_EXPIRY_MINUTES = 10
# Security model for a 6-digit code is short expiry + attempt-lockout + IP
# rate limiting (see core/throttling.py's OtpVerifyRateThrottle) - not hash
# cost - so a plain fast digest is enough to keep it out of the DB in
# plaintext without adding avoidable latency to every verify call.
OTP_MAX_ATTEMPTS = 5


def generate_otp_code():
    return f'{secrets.randbelow(1_000_000):06d}'


def hash_otp_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def issue_otp(user):
    """Generates a fresh code, stores its hash + a new expiry on the user's
    profile (resetting the attempt counter), and returns the plaintext code
    for the caller to email - nothing else ever sees the plaintext."""
    code = generate_otp_code()
    profile = user.profile
    profile.otp_code_hash = hash_otp_code(code)
    profile.otp_expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
    profile.otp_attempts = 0
    profile.save(update_fields=['otp_code_hash', 'otp_expires_at', 'otp_attempts'])
    return code


def _clear_otp(profile):
    profile.otp_code_hash = None
    profile.otp_expires_at = None
    profile.otp_attempts = 0
    profile.save(update_fields=['otp_code_hash', 'otp_expires_at', 'otp_attempts'])


def verify_otp(user, submitted_code):
    """Returns (success, error_code) where error_code is one of 'expired',
    'too_many_attempts', 'incorrect', or '' on success. On success, activates
    the account and clears the OTP fields; on a wrong guess, increments the
    attempt counter (persisted) so lockout survives across requests."""
    profile = user.profile

    if not profile.otp_code_hash or not profile.otp_expires_at:
        return False, 'incorrect'

    if timezone.now() > profile.otp_expires_at:
        return False, 'expired'

    if profile.otp_attempts >= OTP_MAX_ATTEMPTS:
        return False, 'too_many_attempts'

    if not hmac.compare_digest(profile.otp_code_hash, hash_otp_code(submitted_code)):
        profile.otp_attempts += 1
        profile.save(update_fields=['otp_attempts'])
        return False, 'incorrect'

    user.is_active = True
    user.save(update_fields=['is_active'])
    profile.is_verified = True
    profile.save(update_fields=['is_verified'])
    _clear_otp(profile)
    return True, ''
