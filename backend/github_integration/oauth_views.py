import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.cookies import GITHUB_LOGIN_NONCE_COOKIE, clear_github_login_nonce_cookie, set_auth_cookies
from accounts.github_auth import (
    GitHubAccountConflictError,
    GitHubAccountNotEmailVerifiedError,
    find_or_create_user_from_github,
)

from .models import GitHubIntegration
from .serializers import GitHubIntegrationSerializer
from .services.github_client import GitHubAPIError
from .services.oauth_service import GitHubOAuthService, OAuthStateError

logger = logging.getLogger(__name__)


class GitHubLoginView(APIView):
    """GET /api/github/login/ - authenticated (JWT). Returns the GitHub
    authorize URL for the *frontend* to navigate the browser to, rather than
    redirecting here itself: a plain browser navigation to this endpoint
    wouldn't carry the Authorization header needed to know which user is
    connecting, so the frontend must call this via an authenticated fetch
    first, then do `window.location = authorize_url` itself."""

    def get(self, request):
        try:
            url = GitHubOAuthService().build_authorize_url(request.user)
        except ImproperlyConfigured as exc:
            logger.error('github_oauth.not_configured', exc_info=True)
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'authorize_url': url})


class GitHubCallbackView(APIView):
    """GET /api/github/callback/ - GitHub redirects the browser here directly
    (see oauth_service's module docstring for why this can't use JWT auth).
    Always ends in an HTTP redirect back into the SPA, success or failure -
    a raw JSON/DRF response here would strand the user on a bare API URL.

    Serves TWO unrelated flows through this one callback, since a GitHub
    OAuth App only supports a single registered callback URL: an
    already-authenticated user connecting a repo (`purpose=link`, the
    original behavior) and an unauthenticated user logging in/signing up
    (`purpose=login`, new) - disambiguated via the signed `state` payload,
    never via request auth state (there isn't any reliable auth state on a
    plain top-level GET redirect)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        state = request.query_params.get('state')
        is_login = GitHubOAuthService.peek_state_purpose(state) == 'login'
        frontend_target = f'{settings.FRONTEND_URL}/login' if is_login else f'{settings.FRONTEND_URL}/github'

        response = self._handle(request, state, is_login, frontend_target)
        if is_login:
            # Single-use regardless of outcome - a stale nonce cookie must
            # never be reusable for a second callback attempt.
            clear_github_login_nonce_cookie(response)
        return response

    def _handle(self, request, state, is_login, frontend_target):
        error_from_github = request.query_params.get('error')
        if error_from_github:
            logger.info('github_oauth.user_denied_access', extra={'error': error_from_github})
            return redirect(f'{frontend_target}?error=access_denied')

        code = request.query_params.get('code')
        if not code:
            return redirect(f'{frontend_target}?error=missing_code')

        try:
            if is_login:
                return self._complete_login(request, code, state)
            GitHubOAuthService().complete_oauth(code, state)
        except OAuthStateError as exc:
            logger.warning('github_oauth.invalid_state', extra={'error': str(exc)})
            return redirect(f'{frontend_target}?error=invalid_state')
        except ImproperlyConfigured:
            logger.error('github_oauth.not_configured', exc_info=True)
            return redirect(f'{frontend_target}?error=not_configured')
        except GitHubAPIError:
            logger.error('github_oauth.callback_failed', exc_info=True)
            return redirect(f'{frontend_target}?error=github_error')

        return redirect(f'{frontend_target}?connected=true')

    def _complete_login(self, request, code, state):
        """The find-or-create + cookie-issuing half of the login flow - kept
        separate so the shared OAuthStateError/ImproperlyConfigured/
        GitHubAPIError handling in _handle() still covers it (including the
        nonce-mismatch case, which complete_login_oauth raises as
        OAuthStateError), while its own account-linking errors
        (email_not_verified, account_conflict) are handled here since they
        don't apply to the link flow at all."""
        login_target = f'{settings.FRONTEND_URL}/login'
        expected_nonce = request.COOKIES.get(GITHUB_LOGIN_NONCE_COOKIE, '')
        try:
            claims = GitHubOAuthService().complete_login_oauth(code, state, expected_nonce)
            user = find_or_create_user_from_github(claims)
        except GitHubAccountNotEmailVerifiedError:
            return redirect(f'{login_target}?error=email_not_verified')
        except GitHubAccountConflictError:
            return redirect(f'{login_target}?error=account_conflict')

        refresh = RefreshToken.for_user(user)
        target = f'{settings.FRONTEND_URL}/admin' if user.is_staff else f'{settings.FRONTEND_URL}/dashboard'
        response = redirect(target)
        set_auth_cookies(response, access=str(refresh.access_token), refresh=str(refresh))
        return response


class GitHubDisconnectView(APIView):
    def post(self, request):
        GitHubOAuthService().disconnect(request.user)
        return Response({'detail': 'GitHub account disconnected.'})


class GitHubIntegrationStatusView(APIView):
    """GET /api/github/status/ - not in the original endpoint list, added so
    the frontend can render "Connect GitHub" vs "Connected as X" without
    having to infer it from whether the repositories list happens to be empty."""

    def get(self, request):
        try:
            integration = request.user.github_integration
        except GitHubIntegration.DoesNotExist:
            return Response({'connected': False})
        return Response({'connected': True, **GitHubIntegrationSerializer(integration).data})
