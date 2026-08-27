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


class PendingRegistration(models.Model):
    """A signup that has been started but not yet proven.

    Registration used to create the User immediately with is_active=False, so
    typing an address into the signup form was enough to occupy a real account
    row and reserve that email forever - whether or not anyone could read the
    inbox. Someone could burn a stranger's address that way, and the stranger
    had no route to reclaim it. Holding the attempt here instead means a User
    exists only once its OTP has come back.

    Rows are replaced on re-registration and deleted on success, so this table
    holds only in-flight attempts; `purge_expired` clears the ones that were
    abandoned.
    """

    email = models.EmailField(unique=True)
    # Already run through Django's password hasher by the caller - plaintext
    # never reaches this table, exactly as it never reaches auth_user.
    password = models.CharField(max_length=128)
    # Same three OTP fields as Profile, carrying the same meanings, so
    # accounts/otp.py can drive either one. See its module docstring.
    otp_code_hash = models.CharField(max_length=64, blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'PendingRegistration({self.email})'

    @classmethod
    def purge_expired(cls):
        """Drops attempts whose code has expired. Called on the write paths
        rather than from a scheduled job because this project has no scheduler
        (see CLAUDE.md) - and an abandoned row is only a problem when it blocks
        the email's owner from registering, which is precisely a write path.
        """
        from django.utils import timezone
        return cls.objects.filter(otp_expires_at__lt=timezone.now()).delete()
