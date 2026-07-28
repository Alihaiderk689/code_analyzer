import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

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
    a raw JSON/DRF response here would strand the user on a bare API URL."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        frontend_target = f'{settings.FRONTEND_URL}/github'

        error_from_github = request.query_params.get('error')
        if error_from_github:
            logger.info('github_oauth.user_denied_access', extra={'error': error_from_github})
            return redirect(f'{frontend_target}?error=access_denied')

        code = request.query_params.get('code')
        state = request.query_params.get('state')
        if not code:
            return redirect(f'{frontend_target}?error=missing_code')

        try:
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
