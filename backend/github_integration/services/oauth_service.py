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
import secrets

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


class GitHubAccountAlreadyLinkedError(Exception):
    """The GitHub account being authorized is already connected to a
    *different* platform user - `github_user_id` is globally unique (one
    GitHub identity maps to at most one platform user), so this must be
    caught and reported before `update_or_create` hits that constraint as a
    raw IntegrityError."""


def _require_oauth_configured() -> None:
    if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
        raise ImproperlyConfigured(
            'GITHUB_CLIENT_ID/GITHUB_CLIENT_SECRET are not configured - create a GitHub OAuth App '
            'at https://github.com/settings/developers and set them in .env.'
        )


class GitHubOAuthService:
    def build_authorize_url(self, user) -> str:
        _require_oauth_configured()
        state = signing.dumps({'purpose': 'link', 'user_id': user.id}, salt=_STATE_SALT)
        logger.info('github_oauth.authorize_url_issued', extra={'user_id': user.id})
        return build_authorize_url(state)

    def build_authorize_url_for_login(self) -> tuple[str, str]:
        """Same OAuth App/callback as build_authorize_url() above, but for the
        unauthenticated login/signup flow (accounts.views.GitHubLoginInitiateView)
        - there's no user yet to encode into `state`, just a nonce, and the
        scope requested is deliberately narrower (see github_client's
        DEFAULT_OAUTH_SCOPE comment).

        Returns (authorize_url, nonce) - the caller (GitHubLoginInitiateView)
        must set the nonce as a short-lived cookie on its response
        (accounts.cookies.set_github_login_nonce_cookie) so the callback can
        verify the browser completing it is the one that started it. Without
        that binding, a signed-but-unbound `state` only proves *we* issued it,
        not that the browser presenting it is the one we issued it to - which
        is exactly the OAuth login-CSRF class of bug (an attacker completes
        their own authorize step, then hands the resulting code+state to a
        victim to silently log the victim into the attacker's account)."""
        _require_oauth_configured()
        nonce = secrets.token_urlsafe(16)
        state = signing.dumps({'purpose': 'login', 'nonce': nonce}, salt=_STATE_SALT)
        logger.info('github_oauth.login_authorize_url_issued')
        return build_authorize_url(state, scope='read:user user:email'), nonce

    def complete_login_oauth(self, code: str, state: str, expected_nonce: str) -> dict:
        """The login-flow counterpart to complete_oauth() below - exchanges
        `code` and returns the GitHub identity claims for
        accounts.github_auth.find_or_create_user_from_github to turn into a
        platform User. Doesn't touch GitHubIntegration at all: that model is
        for the repo-connect feature, an unrelated concern from "who is this
        person logging in as". `expected_nonce` comes from the browser-bound
        cookie set by build_authorize_url_for_login()'s caller - see its
        docstring for why this check exists."""
        _require_oauth_configured()
        self._require_login_state(state, expected_nonce)

        token_data = GitHubClient.exchange_code_for_token(code)
        access_token = token_data['access_token']
        client = GitHubClient(access_token=access_token)
        profile = client.get_authenticated_user()
        email = client.get_primary_verified_email()

        return {
            'github_id': profile['id'],
            'username': profile['login'],
            'email': email,
            'avatar_url': profile.get('avatar_url'),
        }

    @staticmethod
    def peek_state_purpose(state: str) -> str | None:
        """Best-effort, non-raising read of `purpose` from `state` - used only
        so the callback view can pick which frontend page to redirect *errors*
        to before the real (raising) validation in complete_oauth()/
        complete_login_oauth() runs. Never raises; returns None for anything
        it can't decode."""
        if not state:
            return None
        try:
            data = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE_SECONDS)
        except signing.BadSignature:
            return None
        return data.get('purpose')

    @staticmethod
    def _require_login_state(state: str, expected_nonce: str) -> None:
        if not state:
            raise OAuthStateError('Missing state parameter.')
        if not expected_nonce:
            # No nonce cookie means this browser never initiated a login
            # attempt - either it expired/was already used, or (the case this
            # check exists for) this code+state pair was minted by someone
            # else's browser and handed to this one.
            raise OAuthStateError('Missing or expired GitHub sign-in session - please try again.')
        try:
            data = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE_SECONDS)
        except signing.SignatureExpired as exc:
            raise OAuthStateError('The GitHub authorization link expired - please try signing in again.') from exc
        except signing.BadSignature as exc:
            raise OAuthStateError('Invalid state parameter.') from exc
        if data.get('purpose') != 'login':
            raise OAuthStateError('This authorization link is not valid for signing in.')
        if not secrets.compare_digest(data.get('nonce', ''), expected_nonce):
            raise OAuthStateError('This authorization link was not issued to this browser.')

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

        if data.get('purpose') != 'link':
            raise OAuthStateError('This authorization link is not valid for connecting a repository.')

        try:
            return User.objects.get(pk=data['user_id'])
        except (User.DoesNotExist, KeyError) as exc:
            raise OAuthStateError('Could not resolve the user for this authorization.') from exc
