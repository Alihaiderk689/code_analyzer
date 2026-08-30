"""The async pipeline a webhook delivery triggers: fetch -> analyze -> store
-> comment. Lives here (not in services/) because Celery's autodiscover_tasks()
looks for a top-level tasks.py per app by convention.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from .models import GitHubRepository, PullRequestAnalysis, RepositoryIndex, WebhookEvent
from .services.comment_service import CommentService
from .services.github_client import GitHubAPIError, GitHubAuthError, GitHubRateLimitError
from .services.pr_analysis_service import PRAnalysisService
from .services.repo_index_service import RepositoryIndexService

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
DEFAULT_RETRY_COUNTDOWN_SECONDS = 60


def _resolve_monitored_repository(repository_id, hook_id, *, select_integration=False):
    """`repository_id` (GitHub's numeric repo id) is only unique *per
    integration* (model constraint), not globally - two different users can
    each connect their own GitHub account and monitor the same real repo,
    each with their own webhook. A plain `.get(repository_id=...)` then
    raises `MultipleObjectsReturned`, and a naive `.first()` would silently
    attribute this delivery (and thus the PR review / rate-limit usage / any
    error state) to whichever row happens to sort first - the wrong
    integration's token would post the review, or the wrong integration's
    quotas would be charged.

    `hook_id` (from the delivery's X-GitHub-Hook-ID header, see
    WebhookEvent.hook_id) disambiguates precisely: each monitored repo has
    its *own* webhook (`GitHubRepository.webhook_id`, set at selection time),
    so it identifies exactly which integration's connection this specific
    delivery belongs to. Returns None (treated the same as "not monitored")
    if the ambiguity can't be resolved, rather than guessing."""
    queryset = GitHubRepository.objects.filter(repository_id=repository_id, is_active=True)
    if select_integration:
        queryset = queryset.select_related('integration')
    matches = list(queryset)

    if len(matches) <= 1:
        return matches[0] if matches else None

    if hook_id is not None:
        exact = [r for r in matches if r.webhook_id == hook_id]
        if len(exact) == 1:
            return exact[0]

    logger.warning(
        'github_task.ambiguous_repository',
        extra={'repository_id': repository_id, 'hook_id': hook_id, 'candidate_count': len(matches)},
    )
    return None


def _mark_permanently_failed(pr_analysis: PullRequestAnalysis, webhook_event: WebhookEvent, message: str) -> None:
    pr_analysis.status = PullRequestAnalysis.Status.FAILED
    pr_analysis.error = message[:4000]
    pr_analysis.save(update_fields=['status', 'error', 'updated_at'])
    webhook_event.processed = True
    webhook_event.error = message[:4000]
    webhook_event.save(update_fields=['processed', 'error'])


@shared_task(bind=True, max_retries=MAX_RETRIES)
def process_pull_request_webhook(self, webhook_event_id: int) -> None:
    try:
        webhook_event = WebhookEvent.objects.get(pk=webhook_event_id)
    except WebhookEvent.DoesNotExist:
        logger.error('github_task.webhook_event_missing', extra={'webhook_event_id': webhook_event_id})
        return

    payload = webhook_event.payload
    pr_data = payload['pull_request']
    repository_payload = payload['repository']

    repository = _resolve_monitored_repository(
        repository_payload['id'], webhook_event.hook_id, select_integration=True,
    )
    if repository is None:
        # Repo was deselected between the webhook firing and this task
        # running (or the delivery couldn't be attributed to a specific
        # monitored repo - see _resolve_monitored_repository) - not an
        # error, just nothing to do.
        logger.info('github_task.repository_not_monitored', extra={'repository_id': repository_payload['id']})
        webhook_event.processed = True
        webhook_event.save(update_fields=['processed'])
        return

    integration = repository.integration
    pr_analysis, created = PullRequestAnalysis.objects.get_or_create(
        repository=repository,
        pull_request_number=pr_data['number'],
        commit_sha=pr_data['head']['sha'],
        defaults={
            'title': (pr_data.get('title') or '')[:500],
            'author': (pr_data.get('user') or {}).get('login', ''),
            'status': PullRequestAnalysis.Status.RUNNING,
        },
    )

    if not created and pr_analysis.status == PullRequestAnalysis.Status.COMPLETED:
        # This exact commit was already analyzed - most likely a redelivered
        # webhook that happened to arrive with a *different* delivery_id (see
        # WebhookEvent's docstring). Nothing new to do.
        logger.info('github_task.already_analyzed', extra={'pr_analysis_id': pr_analysis.id})
        webhook_event.processed = True
        webhook_event.save(update_fields=['processed'])
        return

    pr_analysis.status = PullRequestAnalysis.Status.RUNNING
    pr_analysis.save(update_fields=['status', 'updated_at'])

    try:
        access_token = integration.get_access_token()
        file_results = PRAnalysisService().analyze(pr_analysis, access_token)
        CommentService().post_review(pr_analysis, file_results, access_token)

    except GitHubAuthError:
        # Not retryable - a revoked/expired token won't start working on retry.
        integration.token_invalid = True
        integration.save(update_fields=['token_invalid'])
        logger.error('github_task.auth_failed', extra={'pr_analysis_id': pr_analysis.id})
        _mark_permanently_failed(pr_analysis, webhook_event, 'GitHub access token is invalid or was revoked.')
        return

    except GitHubRateLimitError as exc:
        if self.request.retries >= self.max_retries:
            logger.warning('github_task.rate_limit_retries_exhausted', extra={'pr_analysis_id': pr_analysis.id})
            _mark_permanently_failed(pr_analysis, webhook_event, 'GitHub API rate limit exceeded repeatedly.')
            return
        countdown = (
            max(1, exc.reset_at - int(timezone.now().timestamp())) if exc.reset_at else DEFAULT_RETRY_COUNTDOWN_SECONDS
        )
        logger.warning(
            'github_task.rate_limited', extra={'pr_analysis_id': pr_analysis.id, 'retry_in_seconds': countdown},
        )
        raise self.retry(exc=exc, countdown=countdown)

    except GitHubAPIError as exc:
        logger.error('github_task.github_api_error', exc_info=True, extra={'pr_analysis_id': pr_analysis.id})
        if self.request.retries >= self.max_retries:
            _mark_permanently_failed(pr_analysis, webhook_event, str(exc))
            return
        # Exponential backoff: 30s, 60s, 120s.
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))

    except Exception as exc:  # noqa: BLE001 - deliberately broad: any unexpected failure must still leave clean state, not a stuck "running" row and an un-retried webhook
        logger.exception('github_task.unexpected_error', extra={'pr_analysis_id': pr_analysis.id})
        _mark_permanently_failed(pr_analysis, webhook_event, str(exc))
        return

    webhook_event.processed = True
    webhook_event.save(update_fields=['processed'])


def _mark_index_failed(index: RepositoryIndex, message: str) -> None:
    index.status = RepositoryIndex.Status.FAILED
    index.error = message[:4000]
    index.save(update_fields=['status', 'error', 'updated_at'])


@shared_task(bind=True, max_retries=MAX_RETRIES)
def build_repository_index(self, repository_id: int) -> None:
    """Triggered by RepositoryService.select_repository right after a repo is
    selected (fire-and-forget, same as the webhook->task handoff above), and
    by the manual reindex endpoint. Building the dependency graph touches one
    GitHub API call per candidate file (see RepositoryIndexService), so this
    runs off the request path the same way PR review does."""
    try:
        repository = GitHubRepository.objects.select_related('integration').get(
            pk=repository_id, is_active=True,
        )
    except GitHubRepository.DoesNotExist:
        # Deselected (or replaced by selecting a different repo) before this
        # task ran - nothing to index anymore.
        logger.info('github_task.repository_not_monitored_for_indexing', extra={'repository_id': repository_id})
        return

    index, _created = RepositoryIndex.objects.get_or_create(repository=repository)

    try:
        RepositoryIndexService().build(repository)

    except GitHubAuthError:
        repository.integration.token_invalid = True
        repository.integration.save(update_fields=['token_invalid'])
        logger.error('github_task.index_auth_failed', extra={'repository': repository.full_name})
        _mark_index_failed(index, 'GitHub access token is invalid or was revoked.')
        return

    except GitHubRateLimitError as exc:
        if self.request.retries >= self.max_retries:
            logger.warning('github_task.index_rate_limit_retries_exhausted', extra={'repository': repository.full_name})
            _mark_index_failed(index, 'GitHub API rate limit exceeded repeatedly.')
            return
        countdown = (
            max(1, exc.reset_at - int(timezone.now().timestamp())) if exc.reset_at else DEFAULT_RETRY_COUNTDOWN_SECONDS
        )
        logger.warning(
            'github_task.index_rate_limited', extra={'repository': repository.full_name, 'retry_in_seconds': countdown},
        )
        raise self.retry(exc=exc, countdown=countdown)

    except GitHubAPIError as exc:
        logger.error('github_task.index_github_api_error', exc_info=True, extra={'repository': repository.full_name})
        if self.request.retries >= self.max_retries:
            _mark_index_failed(index, str(exc))
            return
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))

    except Exception as exc:  # noqa: BLE001 - see process_pull_request_webhook's identical rationale above
        logger.exception('github_task.index_unexpected_error', extra={'repository': repository.full_name})
        _mark_index_failed(index, str(exc))
        return


@shared_task
def process_push_webhook(webhook_event_id: int) -> None:
    """Triggered by a push to a monitored repository's default branch (see
    WebhookService._should_process) - keeps the dependency-graph index from
    going stale between PRs, since GitHub only sends pull_request events for
    PR activity, not plain pushes. Just re-queues build_repository_index,
    which already owns all the retry/error handling for the actual rebuild -
    this task's only job is resolving the webhook payload to a monitored
    GitHubRepository and marking the delivery processed."""
    try:
        webhook_event = WebhookEvent.objects.get(pk=webhook_event_id)
    except WebhookEvent.DoesNotExist:
        logger.error('github_task.webhook_event_missing', extra={'webhook_event_id': webhook_event_id})
        return

    repository_payload = webhook_event.payload['repository']

    repository = _resolve_monitored_repository(repository_payload['id'], webhook_event.hook_id)
    if repository is None:
        # Deselected (or replaced by selecting a different repo) before this
        # task ran, or the delivery couldn't be attributed to a specific
        # monitored repo (see _resolve_monitored_repository) - nothing to
        # index.
        logger.info('github_task.repository_not_monitored_for_push', extra={'repository_id': repository_payload['id']})
        webhook_event.processed = True
        webhook_event.save(update_fields=['processed'])
        return

    build_repository_index.delay(repository.id)
    webhook_event.processed = True
    webhook_event.save(update_fields=['processed'])
