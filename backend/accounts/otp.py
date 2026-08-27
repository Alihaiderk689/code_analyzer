"""Registration OTP: generation, storage, and verification.

Kept separate from serializers.py/views.py so the hashing/expiry/attempt-
lockout logic has one home, same reasoning as google_auth.py/github_auth.py
being split out from the serializers that call them.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Profile

OTP_EXPIRY_MINUTES = 10
# Security model for a 6-digit code is short expiry + attempt-lockout + IP
# rate limiting (see core/throttling.py's OtpVerifyRateThrottle) - not hash
# cost - so a plain fast digest is enough to keep it out of the DB in
# plaintext without adding avoidable latency to every verify call.
OTP_MAX_ATTEMPTS = 5


def _otp_pepper():
    """Server-side secret keying the OTP HMAC (settings.OTP_PEPPER_KEY, falling
    back to SECRET_KEY). Read per-call rather than at import so override_settings
    works in tests and so a rotated key takes effect on reload.

    Why HMAC rather than a bare digest: the keyspace here is only 10^6, so an
    attacker holding a database dump can exhaust every possible plain SHA-256
    OTP hash in well under a second on commodity hardware. Keying the digest
    with a secret that lives in the environment (not the database) means a
    dump of the DB alone is not enough to turn otp_code_hash back into a
    usable code - the attacker needs the application's secret too.
    """
    return (settings.OTP_PEPPER_KEY or settings.SECRET_KEY).encode()


def generate_otp_code():
    return f'{secrets.randbelow(1_000_000):06d}'


def hash_otp_code(code):
    return hmac.new(_otp_pepper(), code.encode(), hashlib.sha256).hexdigest()


def issue_otp_to(carrier):
    """Generates a fresh code, stores its hash + a new expiry on `carrier`
    (resetting the attempt counter), and returns the plaintext code for the
    caller to email - nothing else ever sees the plaintext.

    `carrier` is any row holding the otp_code_hash/otp_expires_at/otp_attempts
    trio: a Profile (email changes, and legacy accounts) or a
    PendingRegistration (signups, which have no account row yet).
    """
    code = generate_otp_code()
    carrier.otp_code_hash = hash_otp_code(code)
    carrier.otp_expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
    carrier.otp_attempts = 0
    carrier.save(update_fields=['otp_code_hash', 'otp_expires_at', 'otp_attempts'])
    return code


def issue_otp(user):
    """issue_otp_to for the Profile-carried case - see its docstring."""
    return issue_otp_to(user.profile)


def _clear_otp(profile, **extra_fields):
    for field, value in extra_fields.items():
        setattr(profile, field, value)
    profile.otp_code_hash = None
    profile.otp_expires_at = None
    profile.otp_attempts = 0
    profile.save(update_fields=[*extra_fields, 'otp_code_hash', 'otp_expires_at', 'otp_attempts'])


def _check_locked(row, submitted_code):
    """Validates `submitted_code` against an already-locked carrier row.

    Returns '' on success or one of 'expired'/'too_many_attempts'/'incorrect'.
    A wrong guess increments and persists the attempt counter here, so lockout
    survives across requests. Callers must hold SELECT ... FOR UPDATE on `row`
    - see verify_otp's docstring for why that lock is load-bearing.
    """
    if not row.otp_code_hash or not row.otp_expires_at:
        return 'incorrect'

    if timezone.now() > row.otp_expires_at:
        return 'expired'

    if row.otp_attempts >= OTP_MAX_ATTEMPTS:
        return 'too_many_attempts'

    if not hmac.compare_digest(row.otp_code_hash, hash_otp_code(submitted_code)):
        row.otp_attempts += 1
        row.save(update_fields=['otp_attempts'])
        return 'incorrect'

    return ''


def consume_pending_registration(pending, submitted_code, materialize):
    """Verifies a signup's code and turns it into a real account, or not at all.

    `materialize(row)` is called with the locked PendingRegistration once the
    code checks out, inside the same transaction and lock, and must return the
    created User. Running it under the lock is the point: two concurrent
    verifies carrying the same valid code would otherwise both pass the check
    and both try to create the account, and the loser would surface as an
    integrity error rather than a duplicate no-op.

    Returns (user, '') on success or (None, error_code).
    """
    with transaction.atomic():
        row = type(pending).objects.select_for_update().filter(pk=pending.pk).first()
        if row is None:
            return None, 'incorrect'

        error_code = _check_locked(row, submitted_code)
        if error_code:
            return None, error_code

        user = materialize(row)
        row.delete()
        return user, ''


def verify_otp(user, submitted_code):
    """Returns (success, error_code) where error_code is one of 'expired',
    'too_many_attempts', 'incorrect', or '' on success. On success, activates
    the account and clears the OTP fields; on a wrong guess, increments the
    attempt counter (persisted) so lockout survives across requests.

    The whole read-check-write cycle runs inside one transaction with the
    Profile row locked (SELECT ... FOR UPDATE). Without the lock, N concurrent
    verify requests for the same account all read the same otp_attempts value
    and all write back value+1, so N guesses cost one attempt - which is
    exactly the lockout that OTP_MAX_ATTEMPTS exists to enforce being bypassed
    by an attacker who simply fires their guesses in parallel.
    """
    with transaction.atomic():
        try:
            profile = Profile.objects.select_for_update().get(user_id=user.pk)
        except Profile.DoesNotExist:
            return False, 'incorrect'

        error_code = _check_locked(profile, submitted_code)
        if error_code:
            # _check_locked may have persisted an incremented attempt counter;
            # point the caller's cached reverse one-to-one at the row it wrote.
            user.profile = profile
            return False, error_code

        user.is_active = True
        user.save(update_fields=['is_active'])
        _clear_otp(profile, is_verified=True)

    # The caller's `user` was loaded before the lock, so its cached reverse
    # one-to-one still holds the pre-verification Profile - point it at the row
    # this function actually wrote, so `user.profile.is_verified` is correct.
    user.profile = profile
    return True, ''
