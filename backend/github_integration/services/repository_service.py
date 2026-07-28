"""Repository listing/selection - the boundary between 'what the user has
access to on GitHub' (a live API call, not stored) and 'what they've chosen
to monitor' (a GitHubRepository row + a real webhook created on their repo).
"""
from __future__ import annotations

import logging

from django.conf import settings

from ..models import GitHubIntegration, GitHubRepository
from .github_client import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)


class RepositoryService:
    def list_available_repositories(self, integration: GitHubIntegration) -> list[dict]:
        client = GitHubClient(integration.get_access_token())
        return client.list_user_repositories()

    def select_repository(
        self, integration: GitHubIntegration, repository_id: int, repository_name: str,
    ) -> GitHubRepository:
        existing = GitHubRepository.objects.filter(integration=integration, repository_id=repository_id).first()
        if existing and existing.is_active and existing.webhook_id:
            # Already monitored with a live webhook - selecting it again is a
            # no-op, not an error, and mustn't create a second webhook.
            return existing

        # Only one repository monitored at a time per integration - selecting a
        # new one stops monitoring (and removes the webhook from) whatever was
        # active before, rather than accumulating webhooks across repos.
        others = integration.repositories.filter(is_active=True).exclude(repository_id=repository_id)
        for other in others:
            self.deselect_repository(integration, other)

        owner, _, repo = repository_name.partition('/')
        client = GitHubClient(integration.get_access_token())
        webhook_url = f'{settings.GITHUB_WEBHOOK_BASE_URL}/api/webhooks/github/'
        webhook = client.create_webhook(owner, repo, webhook_url, settings.GITHUB_WEBHOOK_SECRET)

        repository, created = GitHubRepository.objects.update_or_create(
            integration=integration,
            repository_id=repository_id,
            defaults={'full_name': repository_name, 'webhook_id': webhook['id'], 'is_active': True},
        )
        logger.info(
            'github_repository.selected',
            # 'created' collides with LogRecord's own built-in `created` (record
            # timestamp) attribute - passing it via extra= raises KeyError.
            extra={'repository': repository_name, 'webhook_id': webhook['id'], 'newly_created': created},
        )
        return repository

    def deselect_repository(self, integration: GitHubIntegration, repository: GitHubRepository) -> None:
        if repository.webhook_id:
            owner, _, repo = repository.full_name.partition('/')
            client = GitHubClient(integration.get_access_token())
            try:
                client.delete_webhook(owner, repo, repository.webhook_id)
            except GitHubAPIError:
                # Best-effort - repo access may already be revoked, the repo
                # may have been deleted/renamed, etc. Either way, we still
                # stop analyzing it on our side.
                logger.warning(
                    'github_repository.webhook_delete_failed', exc_info=True,
                    extra={'repository': repository.full_name, 'webhook_id': repository.webhook_id},
                )

        repository.is_active = False
        repository.webhook_id = None
        repository.save(update_fields=['is_active', 'webhook_id'])
        logger.info('github_repository.deselected', extra={'repository': repository.full_name})
