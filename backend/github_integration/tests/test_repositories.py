from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from ..models import GitHubRepository
from ..services.github_client import GitHubAPIError, GitHubAuthError, GitHubRateLimitError
from ..services.repository_service import RepositoryService
from .factories import TEST_ENCRYPTION_KEY, make_authenticated_client, make_integration, make_repository, make_user

_SETTINGS = dict(
    GITHUB_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    GITHUB_WEBHOOK_BASE_URL='http://localhost:8000', GITHUB_WEBHOOK_SECRET='wh-secret',
)


@override_settings(**_SETTINGS)
class RepositoryServiceTests(TestCase):
    @patch('github_integration.services.repository_service.GitHubClient')
    def test_select_repository_creates_webhook_and_stores_id(self, mock_client_cls):
        integration = make_integration(make_user())
        mock_client_cls.return_value.create_webhook.return_value = {'id': 12345}

        repository = RepositoryService().select_repository(integration, 99, 'octocat/hello-world')

        self.assertEqual(repository.webhook_id, 12345)
        mock_client_cls.return_value.create_webhook.assert_called_once_with(
            'octocat', 'hello-world', 'http://localhost:8000/api/webhooks/github/', 'wh-secret',
        )

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_selecting_already_monitored_repository_is_a_noop(self, mock_client_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=99, webhook_id=555, is_active=True)

        RepositoryService().select_repository(integration, 99, 'octocat/hello-world')

        mock_client_cls.return_value.create_webhook.assert_not_called()

    @patch('github_integration.services.repository_service.GitHubClient')
    def test_reselecting_an_inactive_repository_creates_a_new_webhook(self, mock_client_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=99, webhook_id=None, is_active=False)
        mock_client_cls.return_value.create_webhook.return_value = {'id': 777}

        repository = RepositoryService().select_repository(integration, 99, 'octocat/hello-world')

        self.assertEqual(repository.webhook_id, 777)
        self.assertTrue(repository.is_active)

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
