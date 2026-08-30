"""Repository listing/selection - the boundary between 'what the user has
access to on GitHub' (a live API call, not stored) and 'what they've chosen
to monitor' (a GitHubRepository row + a real webhook created on their repo).
"""
from __future__ import annotations

import logging

from django.conf import settings

from ..models import GitHubIntegration, GitHubRepository
from ..tasks import build_repository_index
from .github_client import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)


class RepositoryAccessDeniedError(Exception):
    """The requested repository_id/repository_name isn't among the repos the
    user's own GitHub token can actually access - raised *before* any webhook
    is created or DB row written. Without this check, the only thing
    stopping a request for a repo the user doesn't control was GitHub's own
    403/404 on the webhook-creation call - relying solely on that is exactly
    the gap this guards against."""


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

        self._verify_user_can_access(integration, repository_id, repository_name)

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

        # Fire-and-forget: builds the file/import graph off the request path
        # (one GitHub call per candidate file - too slow/rate-limit-hungry to
        # do inline). Also re-runs on every select (including re-selecting the
        # same repo) so it's never left stale between here and the first push
        # webhook - see RepositoryReindexView for the manual equivalent, and
        # tasks.process_push_webhook for what keeps it fresh afterward.
        build_repository_index.delay(repository.id)
        return repository

    def _verify_user_can_access(self, integration: GitHubIntegration, repository_id: int, repository_name: str) -> None:
        """Cross-checks the client-supplied repository_id/repository_name
        against the user's own GitHub repos (GET /user/repos, scoped by
        their token) *before* select_repository creates a webhook or DB row
        - rather than finding out only when GitHub itself rejects the
        webhook-creation call. Also catches a mismatched id/name pair (the
        serializer validates each field's shape independently, not that they
        actually refer to the same repo)."""
        available = self.list_available_repositories(integration)
        match = next((repo for repo in available if repo['id'] == repository_id), None)
        if match is None or match['full_name'] != repository_name:
            logger.warning(
                'github_repository.access_denied',
                extra={'repository_id': repository_id, 'repository_name': repository_name},
            )
            raise RepositoryAccessDeniedError(
                f"Repository {repository_name!r} (id={repository_id}) is not accessible with this GitHub account.",
            )

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
