from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .brevo_client import BrevoClient


def send_otp_email(user, code):
    BrevoClient().send_email(
        to_email=user.email,
        subject='Your verification code',
        html_content=(
            f'<p>Hi {user.username},</p>'
            f'<p>Your verification code is:</p>'
            f'<p style="font-size:28px;font-weight:bold;letter-spacing:4px;">{code}</p>'
            f'<p>This code expires in 10 minutes. If you did not create an account, '
            f'you can ignore this email.</p>'
        ),
        text_content=(
            f'Hi {user.username},\n\n'
            f'Your verification code is: {code}\n\n'
            f'This code expires in 10 minutes. If you did not create an account, '
            f'you can ignore this email.'
        ),
    )


def send_password_reset_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}'
    BrevoClient().send_email(
        to_email=user.email,
        subject='Reset your password',
        html_content=(
            f'<p>Hi {user.username},</p>'
            f'<p>Use the link below to reset your password:</p>'
            f'<p><a href="{link}">{link}</a></p>'
            f'<p>If you did not request this, you can ignore this email.</p>'
        ),
        text_content=(
            f'Hi {user.username},\n\n'
            f'Use the link below to reset your password:\n{link}\n\n'
            f'If you did not request this, you can ignore this email.'
        ),
    )
