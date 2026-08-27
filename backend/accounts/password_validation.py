"""Django AUTH_PASSWORD_VALIDATORS entries.

Separate from validators.py, which holds DRF *serializer field* validators
(callables raising rest_framework.serializers.ValidationError). These are the
other kind: classes with validate()/get_help_text(), raising Django's
core.exceptions.ValidationError, run by django.contrib.auth's
validate_password() - which the register/change/reset serializers all call.
"""
from django.conf import settings
from pwned_passwords_django.validators import PwnedPasswordsValidator as _PwnedPasswordsValidator


class PwnedPasswordsValidator(_PwnedPasswordsValidator):
    """Rejects passwords found in the Have I Been Pwned breach corpus, unless
    settings.PWNED_PASSWORDS_ENABLED is False.

    Wrapping the upstream validator instead of listing it directly in
    AUTH_PASSWORD_VALIDATORS buys one thing: an off switch that isn't "delete
    the entry from the settings list". The check costs a live HTTPS request to
    api.pwnedpasswords.com on every password set, which is exactly what you
    don't want on the path of every test that registers a user - so the test
    suite turns it off via the setting while still exercising the validator
    itself (see PwnedPasswordValidatorTests), and the validator stays
    unconditionally present in AUTH_PASSWORD_VALIDATORS where it can be
    asserted on.

    Everything else - the k-anonymity range query (only the first 5 characters
    of the password's SHA-1 ever leave this process), the 1-second timeout,
    and the fall back to Django's CommonPasswordValidator if the API can't be
    reached rather than waving the password through - is upstream behavior,
    unchanged.
    """

    def validate(self, password, user=None):
        if not getattr(settings, 'PWNED_PASSWORDS_ENABLED', True):
            return
        super().validate(password, user)
