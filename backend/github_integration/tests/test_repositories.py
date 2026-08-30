from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from ..models import GitHubRepository, RepositoryContextCheck, RepositoryIndex
from core.execution_budget import (
    REASON_REQUEST_BUDGET_EXHAUSTED,
    STAGE_AI_ENRICHMENT,
    STAGE_BANDIT,
)

from ..services.fetch_budget import TRUNCATED_BUDGET_EXHAUSTED, FetchBudgetExceeded
from ..services.github_client import GitHubAPIError, GitHubAuthError, GitHubFileTooLargeError, GitHubRateLimitError
from ..services.repository_service import RepositoryAccessDeniedError, RepositoryService
from .factories import TEST_ENCRYPTION_KEY, make_authenticated_client, make_integration, make_repository, make_user

_SETTINGS = dict(
    GITHUB_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    GITHUB_WEBHOOK_BASE_URL='http://localhost:8000', GITHUB_WEBHOOK_SECRET='wh-secret',
)


@override_settings(**_SETTINGS)
class RepositoryServiceTests(TestCase):
    @patch('github_integration.services.repository_service.build_repository_index')
    @patch('github_integration.services.repository_service.GitHubClient')
    def test_select_repository_creates_webhook_and_stores_id(self, mock_client_cls, mock_build_index):
        integration = make_integration(make_user())
        mock_client_cls.return_value.create_webhook.return_value = {'id': 12345}
        mock_client_cls.return_value.list_user_repositories.return_value = [
            {'id': 99, 'full_name': 'octocat/hello-world'},
        ]

        repository = RepositoryService().select_repository(integration, 99, 'octocat/hello-world')

        self.assertEqual(repository.webhook_id, 12345)
        mock_client_cls.return_value.create_webhook.assert_called_once_with(
            'octocat', 'hello-world', 'http://localhost:8000/api/webhooks/github/', 'wh-secret',
        )
        mock_build_index.delay.assert_called_once_with(repository.id)

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_selecting_already_monitored_repository_is_a_noop(self, mock_client_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=99, webhook_id=555, is_active=True)

        RepositoryService().select_repository(integration, 99, 'octocat/hello-world')

        mock_client_cls.return_value.create_webhook.assert_not_called()

    @patch('github_integration.services.repository_service.build_repository_index')
    @patch('github_integration.services.repository_service.GitHubClient')
    def test_reselecting_an_inactive_repository_creates_a_new_webhook(self, mock_client_cls, _mock_build_index):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=99, webhook_id=None, is_active=False)
        mock_client_cls.return_value.create_webhook.return_value = {'id': 777}
        mock_client_cls.return_value.list_user_repositories.return_value = [
            {'id': 99, 'full_name': 'octocat/hello-world'},
        ]

        repository = RepositoryService().select_repository(integration, 99, 'octocat/hello-world')

        self.assertEqual(repository.webhook_id, 777)
        self.assertTrue(repository.is_active)

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_selecting_a_repository_not_owned_by_the_user_is_rejected(self, mock_client_cls):
        integration = make_integration(make_user())
        # The user's own GitHub account can't see this repo at all - only
        # *other* repos are returned.
        mock_client_cls.return_value.list_user_repositories.return_value = [
            {'id': 1, 'full_name': 'octocat/some-other-repo'},
        ]

        with self.assertRaises(RepositoryAccessDeniedError):
            RepositoryService().select_repository(integration, 99, 'someone-else/private-repo')

        mock_client_cls.return_value.create_webhook.assert_not_called()
        self.assertFalse(GitHubRepository.objects.filter(repository_id=99).exists())

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_selecting_with_a_mismatched_id_and_name_pair_is_rejected(self, mock_client_cls):
        integration = make_integration(make_user())
        # The user really does own repo 99, but under a different name than
        # what was submitted - a client sending a mismatched pair must not
        # be able to get a webhook created under the wrong name.
        mock_client_cls.return_value.list_user_repositories.return_value = [
            {'id': 99, 'full_name': 'octocat/hello-world'},
        ]

        with self.assertRaises(RepositoryAccessDeniedError):
            RepositoryService().select_repository(integration, 99, 'octocat/a-different-repo')

        mock_client_cls.return_value.create_webhook.assert_not_called()

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_deselect_repository_deletes_webhook_and_deactivates(self, mock_client_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, webhook_id=555, is_active=True)

        RepositoryService().deselect_repository(integration, repository)

        repository.refresh_from_db()
        self.assertFalse(repository.is_active)
        self.assertIsNone(repository.webhook_id)
        mock_client_cls.return_value.delete_webhook.assert_called_once()

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_deselect_repository_succeeds_even_if_webhook_delete_fails(self, mock_client_cls):
        integration = make_integration(make_user())
        repository = make_repository(integration, webhook_id=555, is_active=True)
        mock_client_cls.return_value.delete_webhook.side_effect = GitHubAPIError('gone')

        RepositoryService().deselect_repository(integration, repository)  # must not raise

        repository.refresh_from_db()
        self.assertFalse(repository.is_active)


@override_settings(**_SETTINGS)
class RepositoryListViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-repositories'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_requires_github_connected_first(self):
        client, _user = make_authenticated_client()
        response = client.get(reverse('github-repositories'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('github_integration.repository_views.RepositoryService')
    def test_flags_monitored_repositories(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        make_repository(integration, repository_id=1, is_active=True)
        mock_service_cls.return_value.list_available_repositories.return_value = [
            {'id': 1, 'full_name': 'octocat/monitored', 'private': False, 'default_branch': 'main'},
            {'id': 2, 'full_name': 'octocat/not-monitored', 'private': True, 'default_branch': 'main'},
        ]

        response = client.get(reverse('github-repositories'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_id = {r['repository_id']: r for r in response.data['results']}
        self.assertTrue(by_id[1]['is_monitored'])
        self.assertFalse(by_id[2]['is_monitored'])

    @patch('github_integration.repository_views.RepositoryService')
    def test_expired_token_marks_integration_invalid_and_returns_401(self, mock_service_cls):
        client, user = make_authenticated_client()
        make_integration(user)
        mock_service_cls.return_value.list_available_repositories.side_effect = GitHubAuthError('expired', 401)

        response = client.get(reverse('github-repositories'))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        user.refresh_from_db()
        self.assertTrue(user.github_integration.token_invalid)

    @patch('github_integration.repository_views.RepositoryService')
    def test_rate_limit_returns_429_with_reset_at(self, mock_service_cls):
        client, user = make_authenticated_client()
        make_integration(user)
        mock_service_cls.return_value.list_available_repositories.side_effect = GitHubRateLimitError(
            'limited', reset_at=1700000000,
        )

        response = client.get(reverse('github-repositories'))

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data['reset_at'], 1700000000)

    @patch('github_integration.repository_views.RepositoryService')
    def test_generic_github_error_returns_503(self, mock_service_cls):
        client, user = make_authenticated_client()
        make_integration(user)
        mock_service_cls.return_value.list_available_repositories.side_effect = GitHubAPIError('down', 500)

        response = client.get(reverse('github-repositories'))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


@override_settings(**_SETTINGS)
class RepositorySelectViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().post(reverse('github-repositories-select'), {
            'repository_id': 1, 'repository_name': 'octocat/hello-world',
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_malformed_repository_name(self):
        client, user = make_authenticated_client()
        make_integration(user)
        response = client.post(reverse('github-repositories-select'), {
            'repository_id': 1, 'repository_name': 'not-a-valid-owner-repo-pair',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('github_integration.repository_views.RepositoryService')
    def test_creates_monitored_repository(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        mock_service_cls.return_value.select_repository.return_value = make_repository(
            integration, repository_id=1, full_name='octocat/hello-world', webhook_id=999,
        )

        response = client.post(reverse('github-repositories-select'), {
            'repository_id': 1, 'repository_name': 'octocat/hello-world',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['full_name'], 'octocat/hello-world')

    @patch('github_integration.repository_views.RepositoryService')
    def test_rejects_a_repository_not_accessible_to_the_user(self, mock_service_cls):
        client, user = make_authenticated_client()
        make_integration(user)
        mock_service_cls.return_value.select_repository.side_effect = RepositoryAccessDeniedError('nope')

        response = client.post(reverse('github-repositories-select'), {
            'repository_id': 1, 'repository_name': 'octocat/hello-world',
        })

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(GitHubRepository.objects.exists())


@override_settings(**_SETTINGS)
class MonitoredRepositoryListViewTests(TestCase):
    def test_only_returns_active_repositories_for_this_user(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        make_repository(integration, repository_id=1, full_name='octocat/active', is_active=True)
        make_repository(integration, repository_id=2, full_name='octocat/inactive', is_active=False)

        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        make_repository(other_integration, repository_id=3, full_name='someone/else', is_active=True)

        response = client.get(reverse('github-repositories-monitored'))

        names = [r['full_name'] for r in response.data]
        self.assertEqual(names, ['octocat/active'])


@override_settings(**_SETTINGS)
class RepositoryDeselectViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().delete(reverse('github-repositories-deselect', args=[1]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_404_for_another_users_repository(self):
        client, user = make_authenticated_client()
        make_integration(user)  # must have their own integration to get past the "connect first" check
        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        repository = make_repository(other_integration)

        response = client.delete(reverse('github-repositories-deselect', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('github_integration.repository_views.RepositoryService')
    def test_deselects_and_returns_204(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.delete(reverse('github-repositories-deselect', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mock_service_cls.return_value.deselect_repository.assert_called_once()


@override_settings(**_SETTINGS)
class RepositoryTreeViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-repository-tree', args=[1]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_404_for_another_users_repository(self):
        client, user = make_authenticated_client()
        make_integration(user)
        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        repository = make_repository(other_integration)

        response = client.get(reverse('github-repository-tree', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('github_integration.repository_views.GitHubClient')
    def test_returns_entries_and_truncated_flag(self, mock_client_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_client_cls.return_value.get_repository_tree.return_value = {
            'entries': [{'path': 'app.py', 'type': 'file', 'size': 10}],
            'truncated': True,
        }

        response = client.get(reverse('github-repository-tree', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [{'path': 'app.py', 'type': 'file', 'size': 10}])
        self.assertTrue(response.data['truncated'])

    @patch('github_integration.repository_views.GitHubClient')
    def test_github_api_error_handled(self, mock_client_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_client_cls.return_value.get_repository_tree.side_effect = GitHubAPIError('down', 500)

        response = client.get(reverse('github-repository-tree', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


@override_settings(**_SETTINGS)
class RepositoryFileContentViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-repository-file', args=[1]), {'path': 'app.py'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_404_for_another_users_repository(self):
        client, user = make_authenticated_client()
        make_integration(user)
        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        repository = make_repository(other_integration)

        response = client.get(reverse('github-repository-file', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_path(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.get(reverse('github-repository-file', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_skips_unsupported_file_without_calling_github(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.get(reverse('github-repository-file', args=[repository.pk]), {'path': 'yarn.lock'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['skipped'])
        self.assertEqual(response.data['skip_reason'], 'lock_file')
        self.assertIsNone(response.data['content'])

    @patch('github_integration.repository_views.GitHubClient')
    def test_returns_raw_content_without_analyzing(self, mock_client_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration, full_name='octocat/hello-world')
        mock_client_cls.return_value.get_file_content.return_value = 'print("hi")\n'

        response = client.get(reverse('github-repository-file', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['skipped'])
        self.assertEqual(response.data['content'], 'print("hi")\n')
        self.assertEqual(response.data['language'], 'Python')
        mock_client_cls.return_value.get_file_content.assert_called_once_with(
            'octocat', 'hello-world', 'app.py', repository.default_branch, max_size_bytes=settings.GITHUB_MAX_FILE_SIZE_BYTES,
        )

    @override_settings(GITHUB_MAX_FILE_SIZE_BYTES=5)
    @patch('github_integration.repository_views.GitHubClient')
    def test_flags_oversized_file_as_skipped(self, mock_client_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        # The real GitHubClient now raises this itself, before ever
        # returning oversized content.
        mock_client_cls.return_value.get_file_content.side_effect = GitHubFileTooLargeError(33, 5)

        response = client.get(reverse('github-repository-file', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['skipped'])
        self.assertEqual(response.data['skip_reason'], 'too_large')

    @patch('github_integration.repository_views.GitHubClient')
    def test_github_api_error_handled(self, mock_client_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_client_cls.return_value.get_file_content.side_effect = GitHubAPIError('down', 500)

        response = client.get(reverse('github-repository-file', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


@override_settings(**_SETTINGS)
class RepositoryIndexStatusViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-repository-index', args=[1]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_not_started_when_no_index_exists_yet(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.get(reverse('github-repository-index', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'not_started')

    def test_returns_existing_index_status(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        RepositoryIndex.objects.create(
            repository=repository, status=RepositoryIndex.Status.COMPLETED,
            files_total=10, files_indexed=8, truncated=False,
        )

        response = client.get(reverse('github-repository-index', args=[repository.pk]))

        self.assertEqual(response.data['status'], RepositoryIndex.Status.COMPLETED)
        self.assertEqual(response.data['files_total'], 10)
        self.assertEqual(response.data['files_indexed'], 8)

    def test_404_for_another_users_repository(self):
        client, user = make_authenticated_client()
        make_integration(user)
        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        repository = make_repository(other_integration)

        response = client.get(reverse('github-repository-index', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(**_SETTINGS)
class RepositoryReindexViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().post(reverse('github-repository-reindex', args=[1]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('github_integration.repository_views.build_repository_index')
    def test_queues_index_build_and_returns_202(self, mock_task):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.post(reverse('github-repository-reindex', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task.delay.assert_called_once_with(repository.id)

    @patch('github_integration.repository_views.build_repository_index')
    def test_404_for_another_users_repository(self, _mock_task):
        client, user = make_authenticated_client()
        make_integration(user)
        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        repository = make_repository(other_integration)

        response = client.post(reverse('github-repository-reindex', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


_CONTEXT_RESULT = {
    'path': 'app.py', 'language': 'Python', 'skipped': False, 'skip_reason': None,
    'content': 'print(1)\n', 'issues': [], 'score': 100.0, 'repo_context': '',
    'related': [{'path': 'utils.py', 'language': 'Python', 'relation': 'imports', 'issues': [], 'score': 100.0}],
    'context_truncated': False, 'context_truncated_reason': '', 'degraded_stages': [],
}


@override_settings(**_SETTINGS)
class ContextCheckQuotaViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-context-check-quota'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_defaults_to_full_quota_with_no_checks_yet(self):
        client, _user = make_authenticated_client()

        response = client.get(reverse('github-context-check-quota'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['remaining'], 1)
        self.assertIsNone(response.data['today_check'])


@override_settings(**_SETTINGS)
class RepositoryFileContextAnalyzeViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().post(reverse('github-repository-analyze-file-context', args=[1]), {'path': 'app.py'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_404_for_another_users_repository(self):
        client, user = make_authenticated_client()
        make_integration(user)
        other_integration = make_integration(make_user('other@example.com'), github_user_id=2)
        repository = make_repository(other_integration)

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_path(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_skip_eligible_file_is_free_and_not_persisted(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)

        response = client.post(
            reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'yarn.lock'},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['skipped'])
        self.assertEqual(RepositoryContextCheck.objects.count(), 0)

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_creates_context_check_with_related_files_and_backing_analysis(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = _CONTEXT_RESULT

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data['related']), 1)
        self.assertEqual(response.data['related'][0]['path'], 'utils.py')
        self.assertIsNotNone(response.data['analysis_id'])
        check = RepositoryContextCheck.objects.get()
        self.assertEqual(check.path, 'app.py')
        self.assertEqual(len(check.related), 1)
        self.assertIsNotNone(check.analysis_id)

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_different_file_same_day_returns_429(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = _CONTEXT_RESULT

        client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})
        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'other.py'})

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_reanalyzing_same_file_same_day_is_free(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = _CONTEXT_RESULT

        client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})
        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['cached'])
        mock_service_cls.return_value.analyze_file_with_context.assert_called_once()

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_github_api_error_handled(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.side_effect = GitHubAPIError('down', 500)

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_truncated_context_is_persisted_and_exposed(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = {
            **_CONTEXT_RESULT,
            'context_truncated': True, 'context_truncated_reason': TRUNCATED_BUDGET_EXHAUSTED,
        }

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['context_truncated'])
        self.assertEqual(response.data['context_truncated_reason'], TRUNCATED_BUDGET_EXHAUSTED)
        self.assertEqual(RepositoryContextCheck.objects.get().context_truncated_reason, TRUNCATED_BUDGET_EXHAUSTED)

        # The free "same path again today" response must not read as complete.
        cached = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})
        self.assertTrue(cached.data['cached'])
        self.assertTrue(cached.data['context_truncated'])

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_request_budget_degradation_is_persisted_and_survives_the_cached_response(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = {
            **_CONTEXT_RESULT,
            'context_truncated': True,
            'context_truncated_reason': REASON_REQUEST_BUDGET_EXHAUSTED,
            'degraded_stages': [STAGE_BANDIT, STAGE_AI_ENRICHMENT],
        }
        url = reverse('github-repository-analyze-file-context', args=[repository.pk])

        response = client.post(url, {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['context_truncated_reason'], REASON_REQUEST_BUDGET_EXHAUSTED)
        self.assertEqual(response.data['degraded_stages'], [STAGE_BANDIT, STAGE_AI_ENRICHMENT])
        check = RepositoryContextCheck.objects.get()
        self.assertEqual(check.context_truncated_reason, REASON_REQUEST_BUDGET_EXHAUSTED)
        self.assertEqual(check.degraded_stages, [STAGE_BANDIT, STAGE_AI_ENRICHMENT])

        # The free "same path again today" response must report the same
        # degradation - a cached partial result that reads as complete would
        # be worse than no cache at all.
        cached = client.post(url, {'path': 'app.py'})
        self.assertTrue(cached.data['cached'])
        self.assertTrue(cached.data['context_truncated'])
        self.assertEqual(cached.data['context_truncated_reason'], REASON_REQUEST_BUDGET_EXHAUSTED)
        self.assertEqual(cached.data['degraded_stages'], [STAGE_BANDIT, STAGE_AI_ENRICHMENT])
        mock_service_cls.return_value.analyze_file_with_context.assert_called_once()

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_stage_degradation_without_truncation_is_reported(self, mock_service_cls):
        """Every neighbor was analyzed, but the AI prose was skipped. Not
        truncation - the file coverage is complete - so the two fields must
        disagree rather than both being forced true."""
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = {
            **_CONTEXT_RESULT, 'degraded_stages': [STAGE_AI_ENRICHMENT],
        }

        response = client.post(
            reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'},
        )

        self.assertFalse(response.data['context_truncated'])
        self.assertEqual(response.data['degraded_stages'], [STAGE_AI_ENRICHMENT])

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_untruncated_context_reports_complete(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.return_value = _CONTEXT_RESULT

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertFalse(response.data['context_truncated'])
        self.assertEqual(response.data['context_truncated_reason'], '')
        self.assertEqual(response.data['degraded_stages'], [])

    @patch('github_integration.repository_views.PRAnalysisService')
    def test_budget_exhausted_on_primary_file_is_503_and_not_an_auth_error(self, mock_service_cls):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repository = make_repository(integration)
        mock_service_cls.return_value.analyze_file_with_context.side_effect = FetchBudgetExceeded('out of time')

        response = client.post(reverse('github-repository-analyze-file-context', args=[repository.pk]), {'path': 'app.py'})

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data['context_truncated_reason'], TRUNCATED_BUDGET_EXHAUSTED)
        # Nothing persisted, quota unspent, and the GitHub connection is not
        # marked invalid - this was our deadline, not GitHub's answer.
        self.assertEqual(RepositoryContextCheck.objects.count(), 0)
        integration.refresh_from_db()
        self.assertFalse(integration.token_invalid)
