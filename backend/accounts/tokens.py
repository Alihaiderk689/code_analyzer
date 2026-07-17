from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """Same mechanism as Django's password reset token, but keyed off
    is_verified too so a token is invalidated once it's been used."""

    def _make_hash_value(self, user, timestamp):
        return f'{user.pk}{timestamp}{user.profile.is_verified}'


email_verification_token = EmailVerificationTokenGenerator()
