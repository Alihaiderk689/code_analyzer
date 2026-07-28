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

from ..models import GitHubRepository, PullRequestAnalysis
from ..services.github_client import GitHubAPIError, GitHubAuthError, GitHubRateLimitError
from ..tasks import MAX_RETRIES, process_pull_request_webhook
from .factories import make_integration, make_pr_analysis, make_repository, make_user, make_webhook_event

_PATCHED_SERVICE = 'github_integration.tasks.PRAnalysisService'
_PATCHED_COMMENTS = 'github_integration.tasks.CommentService'

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
