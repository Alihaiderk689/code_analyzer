import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import DjangoUnicodeDecodeError, force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

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
