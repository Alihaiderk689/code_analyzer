from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .tokens import email_verification_token


def send_verification_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)
    link = f'{settings.FRONTEND_URL}/verify-email?uid={uid}&token={token}'
    send_mail(
        subject='Verify your email address',
        message=(
            f'Hi {user.username},\n\n'
            f'Please verify your email by visiting the link below:\n{link}\n\n'
            f'If you did not create an account, you can ignore this email.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )


def send_password_reset_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}'
    send_mail(
        subject='Reset your password',
        message=(
            f'Hi {user.username},\n\n'
            f'Use the link below to reset your password:\n{link}\n\n'
            f'If you did not request this, you can ignore this email.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )
