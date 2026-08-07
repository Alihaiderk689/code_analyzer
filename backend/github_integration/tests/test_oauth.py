from unittest.mock import patch

from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from ..models import GitHubIntegration
from ..services.github_client import GitHubAPIError
from ..services.oauth_service import GitHubOAuthService, OAuthStateError, _STATE_SALT
from .factories import TEST_ENCRYPTION_KEY, make_authenticated_client, make_integration, make_repository, make_user

_OAUTH_SETTINGS = dict(
    GITHUB_CLIENT_ID='test-client-id', GITHUB_CLIENT_SECRET='test-secret',
    GITHUB_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    GITHUB_OAUTH_REDIRECT_URI='http://localhost:8000/api/github/callback/',
    FRONTEND_URL='http://localhost:5173',
)


@override_settings(**_OAUTH_SETTINGS)
class BuildAuthorizeUrlServiceTests(TestCase):
    def test_encodes_user_id_into_signed_state(self):
        user = make_user()
        url = GitHubOAuthService().build_authorize_url(user)
        state = url.split('state=')[1].split('&')[0]
        from urllib.parse import unquote
        data = signing.loads(unquote(state), salt=_STATE_SALT, max_age=600)
        self.assertEqual(data['user_id'], user.id)

    @override_settings(GITHUB_CLIENT_ID='', GITHUB_CLIENT_SECRET='')
    def test_raises_when_oauth_not_configured(self):
        with self.assertRaises(ImproperlyConfigured):
            GitHubOAuthService().build_authorize_url(make_user())


@override_settings(**_OAUTH_SETTINGS)
class CompleteOAuthServiceTests(TestCase):
    def _state_for(self, user):
        return signing.dumps({'purpose': 'link', 'user_id': user.id}, salt=_STATE_SALT)

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_creates_integration_on_success(self, mock_client_cls):
        user = make_user()
        mock_client_cls.exchange_code_for_token.return_value = {'access_token': 'gho_newtoken'}
        mock_client_cls.return_value.get_authenticated_user.return_value = {'id': 555, 'login': 'octocat'}

        integration = GitHubOAuthService().complete_oauth('some-code', self._state_for(user))

        self.assertEqual(integration.user, user)
        self.assertEqual(integration.github_user_id, 555)
        self.assertEqual(integration.username, 'octocat')
        self.assertEqual(integration.get_access_token(), 'gho_newtoken')

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_reconnecting_updates_existing_integration_instead_of_erroring(self, mock_client_cls):
        user = make_user()
        make_integration(user, github_user_id=1, username='old-username')
        mock_client_cls.exchange_code_for_token.return_value = {'access_token': 'gho_updated'}
        mock_client_cls.return_value.get_authenticated_user.return_value = {'id': 1, 'login': 'new-username'}

        integration = GitHubOAuthService().complete_oauth('some-code', self._state_for(user))

        self.assertEqual(GitHubIntegration.objects.filter(user=user).count(), 1)
        self.assertEqual(integration.username, 'new-username')
        self.assertEqual(integration.get_access_token(), 'gho_updated')

    def test_missing_state_raises_oauth_state_error(self):
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_oauth('some-code', '')

    def test_tampered_state_raises_oauth_state_error(self):
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_oauth('some-code', 'tampered.state.value')

    def test_expired_state_raises_oauth_state_error(self):
        user = make_user()
        with patch('django.core.signing.time.time', return_value=1_000_000):
            state = self._state_for(user)
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_oauth('some-code', state)

    def test_state_for_nonexistent_user_raises_oauth_state_error(self):
        state = signing.dumps({'purpose': 'link', 'user_id': 999999}, salt=_STATE_SALT)
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_oauth('some-code', state)


@override_settings(**_OAUTH_SETTINGS)
class LoginOAuthServiceTests(TestCase):
    def test_build_authorize_url_for_login_uses_narrow_scope(self):
        url, _nonce = GitHubOAuthService().build_authorize_url_for_login()
        from urllib.parse import parse_qs, unquote, urlparse
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['scope'][0], 'read:user user:email')

    def test_build_authorize_url_for_login_state_has_login_purpose_and_returned_nonce(self):
        url, nonce = GitHubOAuthService().build_authorize_url_for_login()
        state = url.split('state=')[1].split('&')[0]
        from urllib.parse import unquote
        data = signing.loads(unquote(state), salt=_STATE_SALT, max_age=600)
        self.assertEqual(data['purpose'], 'login')
        self.assertEqual(data['nonce'], nonce)

    @override_settings(GITHUB_CLIENT_ID='', GITHUB_CLIENT_SECRET='')
    def test_build_authorize_url_for_login_raises_when_not_configured(self):
        with self.assertRaises(ImproperlyConfigured):
            GitHubOAuthService().build_authorize_url_for_login()

    def test_link_state_rejected_by_complete_login_oauth(self):
        user = make_user()
        link_state = signing.dumps({'purpose': 'link', 'user_id': user.id}, salt=_STATE_SALT)
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_login_oauth('some-code', link_state, 'nonce')

    def test_login_state_rejected_by_complete_oauth(self):
        login_state = signing.dumps({'purpose': 'login', 'nonce': 'abc'}, salt=_STATE_SALT)
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_oauth('some-code', login_state)

    def test_mismatched_nonce_rejected(self):
        login_state = signing.dumps({'purpose': 'login', 'nonce': 'abc'}, salt=_STATE_SALT)
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_login_oauth('some-code', login_state, 'not-abc')

    def test_missing_expected_nonce_rejected(self):
        login_state = signing.dumps({'purpose': 'login', 'nonce': 'abc'}, salt=_STATE_SALT)
        with self.assertRaises(OAuthStateError):
            GitHubOAuthService().complete_login_oauth('some-code', login_state, '')

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_complete_login_oauth_returns_claims(self, mock_client_cls):
        login_state = signing.dumps({'purpose': 'login', 'nonce': 'abc'}, salt=_STATE_SALT)
        mock_client_cls.exchange_code_for_token.return_value = {'access_token': 'gho_tok'}
        mock_client_cls.return_value.get_authenticated_user.return_value = {
            'id': 42, 'login': 'octocat', 'avatar_url': 'https://example.com/a.png',
        }
        mock_client_cls.return_value.get_primary_verified_email.return_value = 'octocat@example.com'

        claims = GitHubOAuthService().complete_login_oauth('some-code', login_state, 'abc')

        self.assertEqual(claims, {
            'github_id': 42, 'username': 'octocat',
            'email': 'octocat@example.com', 'avatar_url': 'https://example.com/a.png',
        })


class PeekStatePurposeTests(TestCase):
    def test_returns_purpose_for_valid_state(self):
        state = signing.dumps({'purpose': 'login', 'nonce': 'abc'}, salt=_STATE_SALT)
        self.assertEqual(GitHubOAuthService.peek_state_purpose(state), 'login')

    def test_returns_none_for_garbage(self):
        self.assertIsNone(GitHubOAuthService.peek_state_purpose('garbage'))

    def test_returns_none_for_empty(self):
        self.assertIsNone(GitHubOAuthService.peek_state_purpose(''))


@override_settings(**_OAUTH_SETTINGS)
class DisconnectServiceTests(TestCase):
    def test_deletes_integration(self):
        user = make_user()
        make_integration(user)
        GitHubOAuthService().disconnect(user)
        self.assertFalse(GitHubIntegration.objects.filter(user=user).exists())

    def test_noop_when_user_has_no_integration(self):
        user = make_user()
        GitHubOAuthService().disconnect(user)  # must not raise
        self.assertFalse(GitHubIntegration.objects.filter(user=user).exists())

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_deletes_webhooks_for_monitored_repositories(self, mock_client_cls):
        user = make_user()
        integration = make_integration(user)
        make_repository(integration, webhook_id=777)
        GitHubOAuthService().disconnect(user)
        mock_client_cls.return_value.delete_webhook.assert_called_once()

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_webhook_cleanup_failure_does_not_block_disconnect(self, mock_client_cls):
        user = make_user()
        integration = make_integration(user)
        make_repository(integration, webhook_id=777)
        mock_client_cls.return_value.delete_webhook.side_effect = GitHubAPIError('boom')

        GitHubOAuthService().disconnect(user)  # must not raise

        self.assertFalse(GitHubIntegration.objects.filter(user=user).exists())


@override_settings(**_OAUTH_SETTINGS)
class GitHubLoginViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-login'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_authorize_url_for_authenticated_user(self):
        client, _user = make_authenticated_client()
        response = client.get(reverse('github-login'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('authorize_url', response.data)
        self.assertTrue(response.data['authorize_url'].startswith('https://github.com/login/oauth/authorize'))

    @override_settings(GITHUB_CLIENT_ID='', GITHUB_CLIENT_SECRET='')
    def test_returns_503_when_not_configured(self):
        client, _user = make_authenticated_client()
        response = client.get(reverse('github-login'))
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


@override_settings(**_OAUTH_SETTINGS)
class GitHubCallbackViewTests(TestCase):
    def test_github_access_denied_redirects_with_error(self):
        response = APIClient().get(reverse('github-callback'), {'error': 'access_denied'})
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn('error=access_denied', response.url)

    def test_missing_code_redirects_with_error(self):
        response = APIClient().get(reverse('github-callback'))
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn('error=missing_code', response.url)

    def test_invalid_state_redirects_with_error(self):
        response = APIClient().get(reverse('github-callback'), {'code': 'abc', 'state': 'garbage'})
        self.assertIn('error=invalid_state', response.url)

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_successful_callback_redirects_with_connected_true(self, mock_client_cls):
        user = make_user()
        state = signing.dumps({'purpose': 'link', 'user_id': user.id}, salt=_STATE_SALT)
        mock_client_cls.exchange_code_for_token.return_value = {'access_token': 'gho_tok'}
        mock_client_cls.return_value.get_authenticated_user.return_value = {'id': 1, 'login': 'octocat'}

        response = APIClient().get(reverse('github-callback'), {'code': 'abc', 'state': state})

        self.assertIn('connected=true', response.url)
        self.assertTrue(GitHubIntegration.objects.filter(user=user).exists())

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_github_api_failure_redirects_with_github_error(self, mock_client_cls):
        user = make_user()
        state = signing.dumps({'purpose': 'link', 'user_id': user.id}, salt=_STATE_SALT)
        mock_client_cls.exchange_code_for_token.side_effect = GitHubAPIError('boom')

        response = APIClient().get(reverse('github-callback'), {'code': 'abc', 'state': state})

        self.assertIn('error=github_error', response.url)


@override_settings(**_OAUTH_SETTINGS)
class GitHubLoginNonceBindingTests(TestCase):
    """Exercises the real (unmocked) nonce-binding check end to end - closes
    the OAuth login-CSRF gap where a validly-signed `state` alone didn't prove
    the browser completing the callback is the one that started the flow."""

    def _mock_github_identity(self, mock_client_cls, github_id=777, email='nonce@example.com'):
        mock_client_cls.exchange_code_for_token.return_value = {'access_token': 'gho_tok'}
        mock_client_cls.return_value.get_authenticated_user.return_value = {'id': github_id, 'login': 'octocat'}
        mock_client_cls.return_value.get_primary_verified_email.return_value = email

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_matching_nonce_cookie_succeeds(self, mock_client_cls):
        self._mock_github_identity(mock_client_cls)
        from urllib.parse import unquote
        url, nonce = GitHubOAuthService().build_authorize_url_for_login()
        state = unquote(url.split('state=')[1].split('&')[0])
        client = APIClient()
        client.cookies['github_login_nonce'] = nonce

        response = client.get(reverse('github-callback'), {'code': 'abc', 'state': state})

        self.assertTrue(response.url.endswith('/dashboard'))
        self.assertIn('access_token', response.cookies)

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_missing_nonce_cookie_rejected(self, mock_client_cls):
        self._mock_github_identity(mock_client_cls)
        from urllib.parse import unquote
        url, _nonce = GitHubOAuthService().build_authorize_url_for_login()
        state = unquote(url.split('state=')[1].split('&')[0])

        # No cookie set - simulates a victim's browser receiving a code+state
        # pair minted by someone else's (the attacker's) authorize step.
        response = APIClient().get(reverse('github-callback'), {'code': 'abc', 'state': state})

        self.assertIn('error=invalid_state', response.url)
        self.assertNotIn('access_token', response.cookies)

    @patch('github_integration.services.oauth_service.GitHubClient')
    def test_mismatched_nonce_cookie_rejected(self, mock_client_cls):
        self._mock_github_identity(mock_client_cls)
        from urllib.parse import unquote
        url, _nonce = GitHubOAuthService().build_authorize_url_for_login()
        state = unquote(url.split('state=')[1].split('&')[0])
        client = APIClient()
        client.cookies['github_login_nonce'] = 'a-different-nonce-entirely'

        response = client.get(reverse('github-callback'), {'code': 'abc', 'state': state})

        self.assertIn('error=invalid_state', response.url)
        self.assertNotIn('access_token', response.cookies)

    def test_nonce_cookie_cleared_after_login_callback(self):
        login_state = signing.dumps({'purpose': 'login', 'nonce': 'abc'}, salt=_STATE_SALT)
        response = APIClient().get(reverse('github-callback'), {'code': 'abc', 'state': login_state})
        self.assertEqual(response.cookies['github_login_nonce'].value, '')


@override_settings(**_OAUTH_SETTINGS)
class GitHubDisconnectViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().post(reverse('github-disconnect'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_disconnects_and_returns_200(self):
        client, user = make_authenticated_client()
        make_integration(user)
        response = client.post(reverse('github-disconnect'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(GitHubIntegration.objects.filter(user=user).exists())


@override_settings(**_OAUTH_SETTINGS)
class GitHubIntegrationStatusViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-status'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_not_connected_returns_false(self):
        client, _user = make_authenticated_client()
        response = client.get(reverse('github-status'))
        self.assertEqual(response.data, {'connected': False})

    def test_connected_returns_integration_details(self):
        client, user = make_authenticated_client()
        make_integration(user, username='octocat')
        response = client.get(reverse('github-status'))
        self.assertTrue(response.data['connected'])
        self.assertEqual(response.data['username'], 'octocat')
        self.assertNotIn('access_token', response.data)
