import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.middleware.csrf import get_token
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from core.throttling import (
    LoginRateThrottle,
    OtpResendRateThrottle,
    OtpVerifyAccountRateThrottle,
    OtpVerifyRateThrottle,
    PasswordResetRateThrottle,
    RegisterRateThrottle,
)
from github_integration.services.oauth_service import GitHubOAuthService

from .brevo_client import BrevoAPIError
from .cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies, set_github_login_nonce_cookie
from .emails import send_otp_email, send_password_reset_email
from .otp import issue_otp
from .serializers import (
    AvatarUploadSerializer,
    ChangePasswordSerializer,
    DeleteAccountSerializer,
    EmailLoginSerializer,
    ForgotPasswordSerializer,
    GoogleLoginSerializer,
    ProfileSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    ResetPasswordSerializer,
    VerifyOtpSerializer,
)
from .tokens import revoke_all_refresh_tokens

logger = logging.getLogger(__name__)
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
        try:
            with transaction.atomic():
                user = serializer.save()
                code = issue_otp(user)
                send_otp_email(user, code)
        except BrevoAPIError:
            logger.error('accounts.otp_email_failed', exc_info=True)
            return Response(
                {'detail': 'Email service is currently unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {'detail': 'Registration successful. Check your email for a verification code.'},
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


class GoogleLoginView(TokenObtainPairView):
    """Same idea as LoginView - identity is established by a verified Google
    ID token (see GoogleLoginSerializer) instead of email+password, but the
    tokens it produces still get moved into httpOnly cookies the same way."""

    permission_classes = [AllowAny]
    serializer_class = GoogleLoginSerializer
    throttle_classes = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            access = response.data.pop('access')
            refresh = response.data.pop('refresh')
            set_auth_cookies(response, access=access, refresh=refresh)
        return response


class GitHubLoginInitiateView(APIView):
    """GET /api/auth/github/ - unauthenticated, returns the GitHub authorize
    URL for the frontend to navigate to. GitHub has no client-side ID-token
    shortcut like Google's Identity Services, so this kicks off the standard
    redirect flow instead; the actual identity resolution happens in
    github_integration's GitHubCallbackView (shared with the separate
    "connect a repo" flow - see its module docstring for why one OAuth App/
    callback serves both)."""

    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def get(self, request):
        try:
            url, nonce = GitHubOAuthService().build_authorize_url_for_login()
        except ImproperlyConfigured as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        response = Response({'authorize_url': url})
        # Binds the flow to this browser - see build_authorize_url_for_login()'s
        # docstring; GitHubCallbackView verifies this against state's nonce.
        set_github_login_nonce_cookie(response, nonce)
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
        # Whoever prompted this reset may well be locked out of the account
        # *because* someone else is already logged into it, so leaving that
        # someone else's refresh token working would defeat the point of the
        # reset. Nothing is re-issued here: this endpoint is unauthenticated,
        # and the frontend sends the user to the login page afterwards.
        revoke_all_refresh_tokens(user)
        return Response({'detail': 'Password has been reset successfully.'})


class VerifyOtpView(APIView):
    """POST /api/auth/verify-email/ - same route as the old link-click flow,
    now a POST with {email, code} instead of a GET with ?uid=&token= (see
    VerifyOtpSerializer for the actual check). Tightly throttled since a
    6-digit code is brute-forceable in a way a 128-bit token never was -
    otp.py's own attempt-lockout is the primary defense, these are backstops.

    Two throttles, both applied: one keyed by IP (total volume from one
    source), one by IP + target email (how much of that volume can be aimed at
    a single account). See core/throttling.py for why neither subsumes the
    other."""

    permission_classes = [AllowAny]
    throttle_classes = [OtpVerifyRateThrottle, OtpVerifyAccountRateThrottle]

    def post(self, request):
        serializer = VerifyOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({'detail': 'Email verified successfully.'})


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OtpResendRateThrottle]

    def post(self, request):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email__iexact=serializer.validated_data['email']).first()
        if user and not user.profile.is_verified:
            try:
                code = issue_otp(user)
                send_otp_email(user, code)
            except BrevoAPIError:
                logger.error('accounts.otp_email_failed', exc_info=True)
                return Response(
                    {'detail': 'Email service is currently unavailable. Please try again.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        return Response({
            'detail': 'If an account with that email exists and is unverified, a verification code has been sent.',
        })


class ChangePasswordView(APIView):
    """Changing a password revokes every refresh token the account has, then
    immediately issues a fresh pair to *this* request's browser.

    The revoke is the security half: a password change is the standard way to
    boot an attacker (or an old, forgotten device) out of an account, and that
    only works if their refresh token stops being accepted. Re-issuing is the
    usability half - without it the caller keeps a still-valid access token
    for up to its 15-minute lifetime and then gets silently logged out on its
    next refresh, minutes after an action that appeared to succeed."""

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data['new_password'])
        request.user.save()

        revoke_all_refresh_tokens(request.user)
        refresh = RefreshToken.for_user(request.user)
        response = Response({'detail': 'Password changed successfully.'})
        set_auth_cookies(response, access=str(refresh.access_token), refresh=str(refresh))
        return response


class ProfileView(APIView):
    def get(self, request):
        serializer = ProfileSerializer(request.user, context={'request': request})
        return Response(serializer.data)

    def patch(self, request):
        """Changing the email address re-runs verification for the new one.

        Previously `email` was writable while `is_verified` was read-only, so a
        PATCH moved the address and carried the verified flag across to an
        address nobody had proved control of - and future password-reset mail
        followed it there. The account stays active throughout (is_active is
        untouched), so this costs the user a verification step, not access.

        The whole thing runs in one transaction: if the OTP email cannot be
        sent, the address change rolls back too, rather than leaving an account
        pointing at an unverified address with no code on its way.
        """
        serializer = ProfileSerializer(request.user, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)

        new_email = serializer.validated_data.get('email')
        email_changed = new_email is not None and new_email != request.user.email

        try:
            with transaction.atomic():
                serializer.save()
                if email_changed:
                    profile = request.user.profile
                    profile.is_verified = False
                    profile.save(update_fields=['is_verified'])
                    code = issue_otp(request.user)
                    send_otp_email(request.user, code)
        except BrevoAPIError:
            logger.error('accounts.otp_email_failed', exc_info=True)
            return Response(
                {'detail': 'Email service is currently unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

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
