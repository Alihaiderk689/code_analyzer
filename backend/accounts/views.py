from django.contrib.auth import get_user_model
from django.middleware.csrf import get_token
from django.utils.encoding import DjangoUnicodeDecodeError, force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from core.throttling import LoginRateThrottle, PasswordResetRateThrottle, RegisterRateThrottle

from .cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
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


class CsrfCookieView(APIView):
    """GET /api/auth/csrf/ - has no purpose beyond making Django issue the
    (non-httpOnly) csrftoken cookie. The frontend reads it via document.cookie
    and echoes it back as X-CSRFToken on every unsafe cookie-authenticated
    request (see accounts/authentication.py); it calls this once on boot,
    before doing anything that might need it."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        get_token(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [RegisterRateThrottle]

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
    """Same EmailLoginSerializer as before - the difference is entirely in
    what happens to the tokens it produces: they're moved into httpOnly
    cookies rather than left in the JSON body, so frontend JS never has
    access to them (see accounts/cookies.py)."""

    permission_classes = [AllowAny]
    serializer_class = EmailLoginSerializer
    throttle_classes = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            access = response.data.pop('access')
            refresh = response.data.pop('refresh')
            set_auth_cookies(response, access=access, refresh=refresh)
        return response


class RefreshView(APIView):
    """POST /api/auth/refresh/ - reads the refresh token from its httpOnly
    cookie only, never the request body (the whole point is the frontend
    never sees the raw token to send it). Rotation/blacklist-after-rotation
    behavior matches simplejwt's stock TokenRefreshView exactly, by reusing
    its serializer - only where the token comes from and goes to differs."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        refresh_token = request.COOKIES.get(REFRESH_COOKIE)
        if not refresh_token:
            return Response({'detail': 'Refresh token missing.'}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = TokenRefreshSerializer(data={'refresh': refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError:
            response = Response(
                {'detail': 'Invalid or expired refresh token.'}, status=status.HTTP_401_UNAUTHORIZED,
            )
            clear_auth_cookies(response)
            return response

        data = serializer.validated_data
        response = Response({'detail': 'Token refreshed.'})
        set_auth_cookies(response, access=data['access'], refresh=data.get('refresh'))
        return response


class LogoutView(APIView):
    """Always succeeds and clears cookies, whether or not there was a valid
    refresh token to blacklist - the client-side goal ("I am now logged out")
    holds either way, and erroring here just for an already-missing/expired/
    replayed cookie would be a confusing dead end for the user, not a
    meaningful security signal."""

    def post(self, request):
        refresh_token = request.COOKIES.get(REFRESH_COOKIE) or request.data.get('refresh')
        if refresh_token:
            try:
                RefreshToken(refresh_token).blacklist()
            except TokenError:
                pass
        response = Response({'detail': 'Logged out successfully.'})
        clear_auth_cookies(response)
        return response


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRateThrottle]

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
    throttle_classes = [PasswordResetRateThrottle]

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
    throttle_classes = [PasswordResetRateThrottle]

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
