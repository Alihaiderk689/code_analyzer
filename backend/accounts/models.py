from django.conf import settings
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    is_verified = models.BooleanField(default=False)
    avatar = models.ImageField(upload_to='avatars/%Y/%m/', blank=True, null=True)

    def __str__(self):
        return f'Profile({self.user.username})'
