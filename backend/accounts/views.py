from django.contrib.auth import get_user_model
from django.utils.encoding import DjangoUnicodeDecodeError, force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .emails import send_password_reset_email, send_verification_email
from .serializers import (
    AvatarUploadSerializer,
    ChangePasswordSerializer,
    DeleteAccountSerializer,
    EmailLoginSerializer,
    ForgotPasswordSerializer,
    ProfileSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    ResetPasswordSerializer,
)
from .tokens import email_verification_token

User = get_user_model()


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        send_verification_email(user)
        return Response(
            {'detail': 'Registration successful. Check your email to verify your account.'},
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    serializer_class = EmailLoginSerializer


class LogoutView(APIView):
    def post(self, request):
        refresh = request.data.get('refresh')
        if not refresh:
            return Response({'detail': 'Refresh token is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            return Response({'detail': 'Invalid or expired token.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Logged out successfully.'})


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email__iexact=serializer.validated_data['email']).first()
        if user:
            send_password_reset_email(user)
        # Same response whether or not the account exists, to avoid leaking which emails are registered.
        return Response({'detail': 'If an account with that email exists, a reset link has been sent.'})


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        return Response({'detail': 'Password has been reset successfully.'})


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        uidb64 = request.query_params.get('uid')
        token = request.query_params.get('token')
        if not uidb64 or not token:
            return Response({'detail': 'uid and token are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, DjangoUnicodeDecodeError):
            return Response({'detail': 'Invalid verification link.'}, status=status.HTTP_400_BAD_REQUEST)

        if not email_verification_token.check_token(user, token):
            return Response({'detail': 'Invalid or expired verification link.'}, status=status.HTTP_400_BAD_REQUEST)

        profile = user.profile
        if not profile.is_verified:
            profile.is_verified = True
            profile.save(update_fields=['is_verified'])
        return Response({'detail': 'Email verified successfully.'})


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email__iexact=serializer.validated_data['email']).first()
        if user and not user.profile.is_verified:
            send_verification_email(user)
        return Response({
            'detail': 'If an account with that email exists and is unverified, a verification email has been sent.',
        })


class ChangePasswordView(APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data['new_password'])
        request.user.save()
        return Response({'detail': 'Password changed successfully.'})


class ProfileView(APIView):
    def get(self, request):
        serializer = ProfileSerializer(request.user, context={'request': request})
        return Response(serializer.data)

    def patch(self, request):
        serializer = ProfileSerializer(request.user, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request):
        serializer = DeleteAccountSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        request.user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AvatarUploadView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = AvatarUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        profile.avatar = serializer.validated_data['avatar']
        profile.save(update_fields=['avatar'])
        avatar_url = request.build_absolute_uri(profile.avatar.url)
        return Response({'avatar': avatar_url})
