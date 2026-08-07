import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.utils.encoding import DjangoUnicodeDecodeError, force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .google_auth import GoogleTokenError, verify_google_access_token
from .validators import (
    normalize_email,
    validate_password_strength,
    validate_person_name,
    validate_username_format,
)

User = get_user_model()


def _generate_username(email):
    """Django's default User model still requires a unique, non-null username
    internally, but clients only ever deal in email+password. Derive a stable,
    unique handle from the email's local part so that field stays populated."""
    base = re.sub(r'[^a-zA-Z0-9_]', '', email.split('@')[0])[:20] or 'user'
    username = base
    suffix = 1
    while User.objects.filter(username__iexact=username).exists():
        username = f'{base}{suffix}'
        suffix += 1
    return username


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password_strength, validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'password2']

    def validate_email(self, value):
        value = normalize_email(value)
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('This email is already registered.')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({'password2': 'Passwords do not match.'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('password2')
        password = validated_data.pop('password')
        user = User(username=_generate_username(validated_data['email']), **validated_data)
        user.set_password(password)
        user.save()
        return user


class EmailLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        attrs['email'] = normalize_email(attrs['email'])
        user = User.objects.filter(email__iexact=attrs['email']).first()
        if user is None or not user.is_active or not user.check_password(attrs['password']):
            raise serializers.ValidationError('No active account found with the given credentials.')

        update_last_login(None, user)
        refresh = RefreshToken.for_user(user)
        return {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'is_verified': user.profile.is_verified,
            },
        }


class GoogleLoginSerializer(serializers.Serializer):
    """Verifies a Google OAuth access token (from the classic popup flow, see
    google_auth.py's module docstring for why not an ID token) and
    finds-or-creates the matching User, then mints tokens exactly like
    EmailLoginSerializer does. Serves both login and signup - the caller
    doesn't need to know which one it'll turn out to be."""

    access_token = serializers.CharField(write_only=True)

    def validate(self, attrs):
        try:
            claims = verify_google_access_token(attrs['access_token'])
        except GoogleTokenError:
            raise serializers.ValidationError('Invalid Google credential.')

        google_id = claims['sub']
        email = normalize_email(claims['email'])
        email_verified = claims.get('email_verified', False)

        user = User.objects.filter(profile__google_id=google_id).first()

        if user is None:
            # First time we've seen this Google sub - only auto-link/create
            # off the email claim if Google itself vouches it's verified,
            # otherwise anyone could claim an arbitrary email address.
            if not email_verified:
                raise serializers.ValidationError(
                    "Google reports this email isn't verified. Verify it with Google, "
                    "or sign in with a password if you already have an account."
                )

            existing = User.objects.filter(email__iexact=email).first()
            if existing is not None:
                if existing.profile.google_id and existing.profile.google_id != google_id:
                    raise serializers.ValidationError('This account is linked to a different Google account.')
                user = existing
                user.profile.google_id = google_id
                if not user.profile.is_verified:
                    user.profile.is_verified = True
                user.profile.save(update_fields=['google_id', 'is_verified'])
            else:
                try:
                    with transaction.atomic():
                        user = User(
                            username=_generate_username(email),
                            email=email,
                            first_name=claims.get('given_name', '') or '',
                            last_name=claims.get('family_name', '') or '',
                        )
                        user.set_unusable_password()
                        user.save()
                        user.profile.google_id = google_id
                        user.profile.is_verified = True
                        user.profile.save(update_fields=['google_id', 'is_verified'])
                except IntegrityError:
                    # Concurrent duplicate first-login (e.g. a double-click)
                    # racing on the google_id unique constraint.
                    raise serializers.ValidationError('Something went wrong. Please try again.')

        if not user.is_active:
            raise serializers.ValidationError('This account is inactive.')

        update_last_login(None, user)
        refresh = RefreshToken.for_user(user)
        return {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'is_verified': user.profile.is_verified,
            },
        }


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password_strength, validate_password])
    new_password2 = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Old password is incorrect.')
        return value

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password2']:
            raise serializers.ValidationError({'new_password2': 'Passwords do not match.'})
        return attrs


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return normalize_email(value)


class ResendVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return normalize_email(value)


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, validators=[validate_password_strength, validate_password])
    new_password2 = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password2']:
            raise serializers.ValidationError({'new_password2': 'Passwords do not match.'})

        try:
            uid = force_str(urlsafe_base64_decode(attrs['uid']))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, DjangoUnicodeDecodeError):
            raise serializers.ValidationError({'uid': 'Invalid reset link.'})

        if not default_token_generator.check_token(user, attrs['token']):
            raise serializers.ValidationError({'token': 'Invalid or expired reset link.'})

        attrs['user'] = user
        return attrs


class ProfileSerializer(serializers.ModelSerializer):
    is_verified = serializers.BooleanField(source='profile.is_verified', read_only=True)
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'is_verified', 'avatar', 'date_joined']
        read_only_fields = ['id', 'date_joined']

    def get_avatar(self, obj):
        avatar = obj.profile.avatar
        if not avatar:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(avatar.url) if request else avatar.url

    def validate_username(self, value):
        validate_username_format(value)
        if User.objects.filter(username__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('This username is already taken.')
        return value

    def validate_first_name(self, value):
        return validate_person_name(value, 'First name') if value else value

    def validate_last_name(self, value):
        return validate_person_name(value, 'Last name') if value else value

    def validate_email(self, value):
        value = normalize_email(value)
        if User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('This email is already in use.')
        return value


class DeleteAccountSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)

    def validate_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Password is incorrect.')
        return value


class AvatarUploadSerializer(serializers.Serializer):
    avatar = serializers.ImageField()

    def validate_avatar(self, value):
        max_size = 5 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError('Image must be smaller than 5MB.')
        return value
