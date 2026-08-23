from django.conf import settings
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    is_verified = models.BooleanField(default=False)
    avatar = models.ImageField(upload_to='avatars/%Y/%m/', blank=True, null=True)
    google_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    github_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    # Registration OTP (see accounts/otp.py) - hashed, never stored in plaintext.
    # Also reused for "resend" (a resend just re-issues into these same fields).
    otp_code_hash = models.CharField(max_length=64, blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f'Profile({self.user.username})'
