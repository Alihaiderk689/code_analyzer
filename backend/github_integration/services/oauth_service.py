"""Orchestrates the GitHub OAuth App flow.

The tricky part in an API-only, JWT-authenticated SPA (no server-side
sessions) is the callback: GitHub redirects the *browser* straight to
/api/github/callback/ with no way to attach our Authorization header to that
navigation, so the callback can't identify "which user" via JWT. Instead,
/login/ (which *is* called with a JWT, via fetch) encodes the user's id into
a signed, time-limited `state` value using Django's own signing framework
(no extra dependency) - the callback verifies and decodes that instead of
relying on a session.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ImproperlyConfigured

from ..models import GitHubIntegration
from .github_client import GitHubAPIError, GitHubClient, build_authorize_url

logger = logging.getLogger(__name__)

User = get_user_model()

_STATE_SALT = 'github-oauth-state'
_STATE_MAX_AGE_SECONDS = 600  # 10 minutes - long enough to finish the GitHub consent screen


class OAuthStateError(Exception):
    """The `state` round-tripped from GitHub is missing, tampered with, or expired."""


def _require_oauth_configured() -> None:
    if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
        raise ImproperlyConfigured(
            'GITHUB_CLIENT_ID/GITHUB_CLIENT_SECRET are not configured - create a GitHub OAuth App '
            'at https://github.com/settings/developers and set them in .env.'
        )


class GitHubOAuthService:
    def build_authorize_url(self, user) -> str:
        _require_oauth_configured()
        state = signing.dumps({'user_id': user.id}, salt=_STATE_SALT)
        logger.info('github_oauth.authorize_url_issued', extra={'user_id': user.id})
        return build_authorize_url(state)

    def complete_oauth(self, code: str, state: str) -> GitHubIntegration:
        _require_oauth_configured()
        user = self._resolve_user_from_state(state)

        token_data = GitHubClient.exchange_code_for_token(code)
        access_token = token_data['access_token']

        profile = GitHubClient(access_token=access_token).get_authenticated_user()

        integration, created = GitHubIntegration.objects.update_or_create(
            user=user,
            defaults={
                'github_user_id': profile['id'],
                'username': profile['login'],
                'token_invalid': False,
            },
        )
        integration.set_access_token(access_token)
        integration.save(update_fields=['access_token', 'github_user_id', 'username', 'token_invalid'])

        logger.info(
            'github_oauth.connected', extra={
                # 'created' collides with LogRecord's own built-in `created`
                # (record timestamp) attribute - passing it via extra=
                # raises KeyError inside logging.Logger.makeRecord().
                'user_id': user.id, 'github_username': profile['login'], 'newly_created': created,
            },
        )
        return integration

    def disconnect(self, user) -> None:
        try:
            integration = user.github_integration
        except GitHubIntegration.DoesNotExist:
            return

        client = GitHubClient(access_token=integration.get_access_token())
        for repository in integration.repositories.filter(webhook_id__isnull=False):
            owner, _, repo = repository.full_name.partition('/')
            try:
                client.delete_webhook(owner, repo, repository.webhook_id)
            except GitHubAPIError:
                # Best-effort cleanup - a webhook GitHub can't reach us to
                # confirm-delete just becomes an inert, harmless leftover on
                # their side; it must never block the user from disconnecting.
                logger.warning(
                    'github_oauth.webhook_cleanup_failed', exc_info=True,
                    extra={'repository': repository.full_name, 'webhook_id': repository.webhook_id},
                )

        logger.info('github_oauth.disconnected', extra={'user_id': user.id, 'github_username': integration.username})
        integration.delete()

    @staticmethod
    def _resolve_user_from_state(state: str):
        if not state:
            raise OAuthStateError('Missing state parameter.')
        try:
            data = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE_SECONDS)
        except signing.SignatureExpired as exc:
            raise OAuthStateError('The GitHub authorization link expired - please try connecting again.') from exc
        except signing.BadSignature as exc:
            raise OAuthStateError('Invalid state parameter.') from exc

        try:
            return User.objects.get(pk=data['user_id'])
        except (User.DoesNotExist, KeyError) as exc:
            raise OAuthStateError('Could not resolve the user for this authorization.') from exc
