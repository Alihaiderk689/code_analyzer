from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from ..models import FileAnalysis, GitHubRepository, PullRequestAnalysis, WebhookEvent
from .factories import (
    TEST_ENCRYPTION_KEY,
    make_file_analysis,
    make_integration,
    make_pr_analysis,
    make_repository,
    make_user,
)


@override_settings(GITHUB_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class GitHubIntegrationModelTests(TestCase):
    def test_set_and_get_access_token_round_trips(self):
        integration = make_integration(make_user(), access_token='gho_rawtoken123')
        self.assertEqual(integration.get_access_token(), 'gho_rawtoken123')

    def test_access_token_stored_encrypted_not_plaintext(self):
        integration = make_integration(make_user(), access_token='gho_rawtoken123')
        integration.refresh_from_db()
        self.assertNotIn(b'gho_rawtoken123', bytes(integration.access_token))

    def test_one_integration_per_user_enforced_at_db_level(self):
        user = make_user()
        make_integration(user, github_user_id=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_integration(user, github_user_id=2)

    def test_github_user_id_unique_across_users(self):
        make_integration(make_user('a@example.com'), github_user_id=42)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_integration(make_user('b@example.com'), github_user_id=42)


class GitHubRepositoryModelTests(TestCase):
    def test_unique_repository_per_integration_enforced(self):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=99)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_repository(integration, repository_id=99, full_name='octocat/other-name')

    def test_same_repository_id_allowed_for_different_integrations(self):
        repo_a = make_repository(make_integration(make_user('a@example.com'), github_user_id=1), repository_id=99)
        repo_b = make_repository(make_integration(make_user('b@example.com'), github_user_id=2), repository_id=99)
        self.assertNotEqual(repo_a.pk, repo_b.pk)

    def test_ordered_newest_first(self):
        integration = make_integration(make_user())
        older = make_repository(integration, repository_id=1, full_name='octocat/older')
        newer = make_repository(integration, repository_id=2, full_name='octocat/newer')
        self.assertEqual(list(GitHubRepository.objects.all()), [newer, older])


class PullRequestAnalysisModelTests(TestCase):
    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))

    def test_unique_per_repository_pr_number_and_commit(self):
        make_pr_analysis(self.repository, pull_request_number=5, commit_sha='a' * 40)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_pr_analysis(self.repository, pull_request_number=5, commit_sha='a' * 40)

    def test_new_commit_on_same_pr_gets_its_own_row(self):
        first = make_pr_analysis(self.repository, pull_request_number=5, commit_sha='a' * 40)
        second = make_pr_analysis(self.repository, pull_request_number=5, commit_sha='b' * 40)
        self.assertNotEqual(first.pk, second.pk)

    def test_default_status_is_pending(self):
        pr_analysis = make_pr_analysis(self.repository)
        self.assertEqual(pr_analysis.status, PullRequestAnalysis.Status.PENDING)


class FileAnalysisModelTests(TestCase):
    def test_issues_defaults_to_empty_list(self):
        pr_analysis = make_pr_analysis(make_repository(make_integration(make_user())))
        file_analysis = FileAnalysis.objects.create(pull_request_analysis=pr_analysis, file_path='app.py')
        self.assertEqual(file_analysis.issues, [])

    def test_ordered_by_file_path(self):
        pr_analysis = make_pr_analysis(make_repository(make_integration(make_user())))
        make_file_analysis(pr_analysis, file_path='z_file.py')
        make_file_analysis(pr_analysis, file_path='a_file.py')
        self.assertEqual(
            list(pr_analysis.file_analyses.values_list('file_path', flat=True)),
            ['a_file.py', 'z_file.py'],
        )


class WebhookEventModelTests(TestCase):
    def test_delivery_id_unique(self):
        WebhookEvent.objects.create(event_type='pull_request', delivery_id='dup-1', payload={})
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookEvent.objects.create(event_type='pull_request', delivery_id='dup-1', payload={})
