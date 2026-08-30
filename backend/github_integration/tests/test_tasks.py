"""Tests the Celery task via .apply() rather than .delay()/.apply_async() -
.apply() runs the task fully in-process, synchronously, with no broker
required, while still exercising Celery's real retry machinery: Task.retry()
raises a Retry exception, and Task.apply() specifically catches it and calls
`retval.sig.apply(retries=retries + 1)` recursively (see
celery/app/task.py) - so retry-exhaustion behavior is tested for real, not
simulated, and nothing here actually sleeps for the retry countdown.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from ..models import GitHubRepository, PullRequestAnalysis, RepositoryIndex
from ..services.github_client import GitHubAPIError, GitHubAuthError, GitHubRateLimitError
from ..tasks import MAX_RETRIES, build_repository_index, process_pull_request_webhook, process_push_webhook
from .factories import make_integration, make_pr_analysis, make_repository, make_user, make_webhook_event

_PATCHED_SERVICE = 'github_integration.tasks.PRAnalysisService'
_PATCHED_COMMENTS = 'github_integration.tasks.CommentService'
_PATCHED_INDEX_SERVICE = 'github_integration.tasks.RepositoryIndexService'

# Task.apply()'s automatic retry-recursion (retval.sig.apply(retries=retries+1)
# in celery/app/task.py) only kicks in when the Retry exception is captured
# into the result rather than re-raised - which happens when
# task_eager_propagates is False. Celery re-reads this from Django settings on
# every app.conf access (verified directly against the installed celery
# version), so overriding it here reliably reaches every recursive retry, not
# just the first .apply() call - a per-call throw=False only covers the
# outermost .apply(), since celery's own recursive re-apply doesn't forward it.
@override_settings(CELERY_TASK_EAGER_PROPAGATES=False)
class _TaskTestCase(TestCase):
    pass


class MissingWebhookEventTests(_TaskTestCase):
    def test_missing_webhook_event_id_returns_without_raising(self):
        result = process_pull_request_webhook.apply(args=[999999], throw=False)
        self.assertFalse(result.failed())


class UnmonitoredRepositoryTests(_TaskTestCase):
    def test_repository_not_found_marks_event_processed_and_creates_nothing(self):
        event = make_webhook_event(repository_id=404404)
        process_pull_request_webhook.apply(args=[event.id], throw=False)
        event.refresh_from_db()
        self.assertTrue(event.processed)
        self.assertEqual(PullRequestAnalysis.objects.count(), 0)

    def test_deselected_repository_is_treated_as_unmonitored(self):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=False)
        event = make_webhook_event(repository_id=2001)
        process_pull_request_webhook.apply(args=[event.id], throw=False)
        event.refresh_from_db()
        self.assertTrue(event.processed)
        self.assertEqual(PullRequestAnalysis.objects.count(), 0)


class AmbiguousRepositoryTests(_TaskTestCase):
    """Two different users can each connect their own GitHub account and
    monitor the same real repository_id (e.g. two collaborators on a shared
    repo) - repository_id is only unique *per integration*, not globally.
    Each has their own webhook, so the delivery's X-GitHub-Hook-ID header
    (WebhookEvent.hook_id) is what disambiguates which one this delivery
    belongs to."""

    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_pull_request_webhook_resolves_via_hook_id_not_first_match(self, mock_service_cls, _mock_comments_cls):
        integration_a = make_integration(make_user('a@example.com'), github_user_id=101)
        integration_b = make_integration(make_user('b@example.com'), github_user_id=102)
        repo_a = make_repository(integration_a, repository_id=5001, webhook_id=111)
        repo_b = make_repository(integration_b, repository_id=5001, webhook_id=222)
        mock_service_cls.return_value.analyze.return_value = []

        event = make_webhook_event(repository_id=5001, hook_id=222)
        process_pull_request_webhook.apply(args=[event.id], throw=False)

        # Only repo_b's (the one matching the delivering webhook) analysis
        # should have been created - never repo_a's, and never both.
        self.assertEqual(PullRequestAnalysis.objects.filter(repository=repo_b).count(), 1)
        self.assertEqual(PullRequestAnalysis.objects.filter(repository=repo_a).count(), 0)

    def test_pull_request_webhook_without_a_resolvable_hook_id_is_a_noop(self):
        integration_a = make_integration(make_user('a@example.com'), github_user_id=101)
        integration_b = make_integration(make_user('b@example.com'), github_user_id=102)
        make_repository(integration_a, repository_id=5002, webhook_id=111)
        make_repository(integration_b, repository_id=5002, webhook_id=222)

        # No hook_id at all (e.g. header missing) - can't disambiguate, so
        # this must not guess and attribute the delivery to either one.
        event = make_webhook_event(repository_id=5002, hook_id=None)
        process_pull_request_webhook.apply(args=[event.id], throw=False)

        event.refresh_from_db()
        self.assertTrue(event.processed)
        self.assertEqual(PullRequestAnalysis.objects.count(), 0)

    def test_push_webhook_resolves_via_hook_id_not_first_match(self):
        integration_a = make_integration(make_user('a@example.com'), github_user_id=101)
        integration_b = make_integration(make_user('b@example.com'), github_user_id=102)
        repo_a = make_repository(integration_a, repository_id=5003, webhook_id=111, default_branch='main')
        repo_b = make_repository(integration_b, repository_id=5003, webhook_id=222, default_branch='main')

        event = make_webhook_event(
            event_type='push', repository_id=5003, hook_id=222,
            repository_full_name=repo_b.full_name,
        )
        with patch('github_integration.tasks.build_repository_index') as mock_index:
            process_push_webhook.apply(args=[event.id], throw=False)

        mock_index.delay.assert_called_once_with(repo_b.id)


class AlreadyAnalyzedTests(_TaskTestCase):
    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_completed_commit_is_not_reanalyzed(self, mock_service_cls, mock_comments_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        make_pr_analysis(
            repository, pull_request_number=1, commit_sha='a' * 40,
            status=PullRequestAnalysis.Status.COMPLETED,
        )
        event = make_webhook_event(repository_id=2001, pr_number=1, sha='a' * 40)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        mock_service_cls.return_value.analyze.assert_not_called()
        mock_comments_cls.return_value.post_review.assert_not_called()
        event.refresh_from_db()
        self.assertTrue(event.processed)

    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_new_commit_on_same_pr_is_analyzed(self, mock_service_cls, _mock_comments_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        make_pr_analysis(
            repository, pull_request_number=1, commit_sha='a' * 40,
            status=PullRequestAnalysis.Status.COMPLETED,
        )
        mock_service_cls.return_value.analyze.return_value = []
        event = make_webhook_event(repository_id=2001, pr_number=1, sha='b' * 40)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        mock_service_cls.return_value.analyze.assert_called_once()
        self.assertEqual(
            PullRequestAnalysis.objects.filter(repository=repository, pull_request_number=1).count(), 2,
        )


class HappyPathTests(_TaskTestCase):
    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_analyzes_and_posts_review_then_marks_event_processed(self, mock_service_cls, mock_comments_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.analyze.return_value = []
        event = make_webhook_event(repository_id=2001, pr_number=1, sha='a' * 40, title='My PR', author='octocat')

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        pr_analysis = PullRequestAnalysis.objects.get(repository__repository_id=2001, pull_request_number=1)
        self.assertEqual(pr_analysis.title, 'My PR')
        self.assertEqual(pr_analysis.author, 'octocat')
        mock_service_cls.return_value.analyze.assert_called_once_with(pr_analysis, integration.get_access_token())
        mock_comments_cls.return_value.post_review.assert_called_once()
        event.refresh_from_db()
        self.assertTrue(event.processed)


class AuthErrorTests(_TaskTestCase):
    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_auth_error_marks_integration_invalid_and_fails_without_retry(self, mock_service_cls, _mock_comments_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.analyze.side_effect = GitHubAuthError('revoked', 401)
        event = make_webhook_event(repository_id=2001)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.analyze.call_count, 1)
        integration.refresh_from_db()
        self.assertTrue(integration.token_invalid)
        pr_analysis = PullRequestAnalysis.objects.get(repository__repository_id=2001)
        self.assertEqual(pr_analysis.status, PullRequestAnalysis.Status.FAILED)
        event.refresh_from_db()
        self.assertTrue(event.processed)
        self.assertTrue(event.error)


class RateLimitRetryTests(_TaskTestCase):
    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_retries_then_permanently_fails_after_max_retries(self, mock_service_cls, _mock_comments_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.analyze.side_effect = GitHubRateLimitError('limited', reset_at=None)
        event = make_webhook_event(repository_id=2001)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        # 1 initial attempt + MAX_RETRIES retries, all exhausted synchronously.
        self.assertEqual(mock_service_cls.return_value.analyze.call_count, MAX_RETRIES + 1)
        pr_analysis = PullRequestAnalysis.objects.get(repository__repository_id=2001)
        self.assertEqual(pr_analysis.status, PullRequestAnalysis.Status.FAILED)
        event.refresh_from_db()
        self.assertTrue(event.processed)

    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_succeeds_on_a_later_retry_without_exhausting(self, mock_service_cls, mock_comments_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.analyze.side_effect = [
            GitHubRateLimitError('limited', reset_at=None), [],
        ]
        event = make_webhook_event(repository_id=2001)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.analyze.call_count, 2)
        pr_analysis = PullRequestAnalysis.objects.get(repository__repository_id=2001)
        self.assertNotEqual(pr_analysis.status, PullRequestAnalysis.Status.FAILED)
        mock_comments_cls.return_value.post_review.assert_called_once()


class GenericGitHubApiErrorRetryTests(_TaskTestCase):
    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_retries_with_backoff_then_permanently_fails(self, mock_service_cls, _mock_comments_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.analyze.side_effect = GitHubAPIError('down', 500)
        event = make_webhook_event(repository_id=2001)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.analyze.call_count, MAX_RETRIES + 1)
        pr_analysis = PullRequestAnalysis.objects.get(repository__repository_id=2001)
        self.assertEqual(pr_analysis.status, PullRequestAnalysis.Status.FAILED)


class UnexpectedErrorTests(_TaskTestCase):
    @patch(_PATCHED_COMMENTS)
    @patch(_PATCHED_SERVICE)
    def test_unexpected_exception_fails_immediately_without_retry(self, mock_service_cls, _mock_comments_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.analyze.side_effect = ValueError('completely unexpected')
        event = make_webhook_event(repository_id=2001)

        process_pull_request_webhook.apply(args=[event.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.analyze.call_count, 1)
        pr_analysis = PullRequestAnalysis.objects.get(repository__repository_id=2001)
        self.assertEqual(pr_analysis.status, PullRequestAnalysis.Status.FAILED)
        self.assertIn('completely unexpected', pr_analysis.error)


class BuildRepositoryIndexMissingRepositoryTests(_TaskTestCase):
    def test_missing_repository_id_returns_without_raising(self):
        result = build_repository_index.apply(args=[999999], throw=False)
        self.assertFalse(result.failed())

    def test_deselected_repository_is_a_noop(self):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=False)

        result = build_repository_index.apply(args=[repository.id], throw=False)

        self.assertFalse(result.failed())
        self.assertEqual(RepositoryIndex.objects.count(), 0)


class BuildRepositoryIndexHappyPathTests(_TaskTestCase):
    @patch(_PATCHED_INDEX_SERVICE)
    def test_calls_build_once_with_the_repository(self, mock_service_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)

        build_repository_index.apply(args=[repository.id], throw=False)

        mock_service_cls.return_value.build.assert_called_once_with(repository)


class BuildRepositoryIndexAuthErrorTests(_TaskTestCase):
    @patch(_PATCHED_INDEX_SERVICE)
    def test_auth_error_marks_integration_invalid_and_index_failed_without_retry(self, mock_service_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.build.side_effect = GitHubAuthError('revoked', 401)

        build_repository_index.apply(args=[repository.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.build.call_count, 1)
        integration.refresh_from_db()
        self.assertTrue(integration.token_invalid)
        index = RepositoryIndex.objects.get(repository=repository)
        self.assertEqual(index.status, RepositoryIndex.Status.FAILED)


class BuildRepositoryIndexRateLimitRetryTests(_TaskTestCase):
    @patch(_PATCHED_INDEX_SERVICE)
    def test_retries_then_permanently_fails_after_max_retries(self, mock_service_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.build.side_effect = GitHubRateLimitError('limited', reset_at=None)

        build_repository_index.apply(args=[repository.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.build.call_count, MAX_RETRIES + 1)
        index = RepositoryIndex.objects.get(repository=repository)
        self.assertEqual(index.status, RepositoryIndex.Status.FAILED)


class BuildRepositoryIndexGenericAPIErrorTests(_TaskTestCase):
    @patch(_PATCHED_INDEX_SERVICE)
    def test_retries_with_backoff_then_permanently_fails(self, mock_service_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.build.side_effect = GitHubAPIError('down', 500)

        build_repository_index.apply(args=[repository.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.build.call_count, MAX_RETRIES + 1)
        index = RepositoryIndex.objects.get(repository=repository)
        self.assertEqual(index.status, RepositoryIndex.Status.FAILED)


class BuildRepositoryIndexUnexpectedErrorTests(_TaskTestCase):
    @patch(_PATCHED_INDEX_SERVICE)
    def test_unexpected_exception_fails_immediately_without_retry(self, mock_service_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        mock_service_cls.return_value.build.side_effect = ValueError('completely unexpected')

        build_repository_index.apply(args=[repository.id], throw=False)

        self.assertEqual(mock_service_cls.return_value.build.call_count, 1)
        index = RepositoryIndex.objects.get(repository=repository)
        self.assertEqual(index.status, RepositoryIndex.Status.FAILED)
        self.assertIn('completely unexpected', index.error)


_PATCHED_BUILD_INDEX = 'github_integration.tasks.build_repository_index'


class ProcessPushWebhookMissingEventTests(_TaskTestCase):
    def test_missing_webhook_event_id_returns_without_raising(self):
        result = process_push_webhook.apply(args=[999999], throw=False)
        self.assertFalse(result.failed())


class ProcessPushWebhookUnmonitoredRepositoryTests(_TaskTestCase):
    @patch(_PATCHED_BUILD_INDEX)
    def test_repository_not_found_marks_event_processed_without_queuing_index_build(self, mock_build_index):
        event = make_webhook_event(event_type='push', repository_id=404404)
        process_push_webhook.apply(args=[event.id], throw=False)
        event.refresh_from_db()
        self.assertTrue(event.processed)
        mock_build_index.delay.assert_not_called()

    @patch(_PATCHED_BUILD_INDEX)
    def test_deselected_repository_is_treated_as_unmonitored(self, mock_build_index):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=False)
        event = make_webhook_event(event_type='push', repository_id=2001)
        process_push_webhook.apply(args=[event.id], throw=False)
        event.refresh_from_db()
        self.assertTrue(event.processed)
        mock_build_index.delay.assert_not_called()


class ProcessPushWebhookHappyPathTests(_TaskTestCase):
    @patch(_PATCHED_BUILD_INDEX)
    def test_queues_index_rebuild_for_monitored_repository_and_marks_event_processed(self, mock_build_index):
        integration = make_integration(make_user())
        repository = make_repository(integration, repository_id=2001, is_active=True)
        event = make_webhook_event(event_type='push', repository_id=2001)

        process_push_webhook.apply(args=[event.id], throw=False)

        mock_build_index.delay.assert_called_once_with(repository.id)
        event.refresh_from_db()
        self.assertTrue(event.processed)
