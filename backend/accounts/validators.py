import re

from rest_framework import serializers

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '-]*$")
USERNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_]*$')
OTP_CODE_RE = re.compile(r'^\d{6}$')


def validate_person_name(value, field_label='This field'):
    value = value.strip()
    if len(value) < 2 or len(value) > 50:
        raise serializers.ValidationError(f'{field_label} must be 2-50 characters.')
    if not NAME_RE.match(value):
        raise serializers.ValidationError(
            f'{field_label} may only contain letters, spaces, hyphens, and apostrophes.'
        )
    return value


def validate_username_format(value):
    if len(value) < 3 or len(value) > 20:
        raise serializers.ValidationError('Username must be 3-20 characters.')
    if value.startswith('_'):
        raise serializers.ValidationError('Username cannot start with an underscore.')
    if not USERNAME_RE.match(value):
        raise serializers.ValidationError('Username may only contain letters, numbers, and underscores.')
    return value


def validate_password_strength(value):
    if len(value) < 8 or len(value) > 128:
        raise serializers.ValidationError('Password must be 8-128 characters.')
    if not re.search(r'[A-Z]', value):
        raise serializers.ValidationError('Password must contain at least one uppercase letter.')
    if not re.search(r'[a-z]', value):
        raise serializers.ValidationError('Password must contain at least one lowercase letter.')
    if not re.search(r'\d', value):
        raise serializers.ValidationError('Password must contain at least one number.')
    if not re.search(r'[^A-Za-z0-9]', value):
        raise serializers.ValidationError('Password must contain at least one special character.')
    return value


def normalize_email(value):
    return value.strip().lower()


def validate_otp_code(value):
    if not OTP_CODE_RE.match(value):
        raise serializers.ValidationError('Enter the 6-digit code.')
    return value
