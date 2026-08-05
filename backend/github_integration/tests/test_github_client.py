from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from ..services.github_client import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubClient,
    GitHubRateLimitError,
    build_authorize_url,
)


def _mock_response(status_code=200, json_data=None, text='', headers=None, links=None):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.json.return_value = json_data if json_data is not None else {}
    response.text = text
    response.headers = headers or {}
    response.links = links or {}
    return response


@override_settings(
    GITHUB_CLIENT_ID='test-client-id', GITHUB_OAUTH_REDIRECT_URI='http://localhost:8000/api/github/callback/',
)
class BuildAuthorizeUrlTests(SimpleTestCase):
    def test_includes_client_id_state_and_redirect_uri(self):
        url = build_authorize_url('signed-state-value')
        self.assertIn('client_id=test-client-id', url)
        self.assertIn('state=signed-state-value', url)
        self.assertIn('redirect_uri=', url)
        self.assertTrue(url.startswith('https://github.com/login/oauth/authorize?'))


class GitHubClientRequestTests(SimpleTestCase):
    @patch('github_integration.services.github_client.requests.request')
    def test_successful_get_returns_json(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data={'login': 'octocat'})
        result = GitHubClient(access_token='tok').get_authenticated_user()
        self.assertEqual(result, {'login': 'octocat'})

    @patch('github_integration.services.github_client.requests.request')
    def test_sends_bearer_token_when_authenticated(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data={})
        GitHubClient(access_token='my-token').get_authenticated_user()
        _args, kwargs = mock_request.call_args
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer my-token')

    @patch('github_integration.services.github_client.requests.request')
    def test_no_authorization_header_when_unauthenticated(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data=[])
        GitHubClient().list_user_repositories()
        _args, kwargs = mock_request.call_args
        self.assertNotIn('Authorization', kwargs['headers'])

    @patch('github_integration.services.github_client.requests.request')
    def test_network_error_raises_github_api_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError('boom')
        with self.assertRaises(GitHubAPIError):
            GitHubClient(access_token='tok').get_authenticated_user()

    @patch('github_integration.services.github_client.requests.request')
    def test_401_raises_github_auth_error(self, mock_request):
        mock_request.return_value = _mock_response(401, json_data={'message': 'Bad credentials'})
        with self.assertRaises(GitHubAuthError):
            GitHubClient(access_token='bad-token').get_authenticated_user()

    @patch('github_integration.services.github_client.requests.request')
    def test_403_with_exhausted_rate_limit_raises_rate_limit_error(self, mock_request):
        mock_request.return_value = _mock_response(
            403, json_data={'message': 'rate limited'},
            headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1700000000'},
        )
        with self.assertRaises(GitHubRateLimitError) as ctx:
            GitHubClient(access_token='tok').get_authenticated_user()
        self.assertEqual(ctx.exception.reset_at, 1700000000)

    @patch('github_integration.services.github_client.requests.request')
    def test_403_without_exhausted_rate_limit_raises_generic_api_error(self, mock_request):
        # A plain permissions 403 (not rate-limiting) must not be misclassified
        # as a rate-limit error - only X-RateLimit-Remaining: 0 means that.
        mock_request.return_value = _mock_response(403, json_data={'message': 'Forbidden'}, headers={})
        with self.assertRaises(GitHubAPIError) as ctx:
            GitHubClient(access_token='tok').get_authenticated_user()
        self.assertNotIsInstance(ctx.exception, GitHubRateLimitError)

    @patch('github_integration.services.github_client.requests.request')
    def test_500_raises_generic_api_error_with_status_code(self, mock_request):
        mock_request.return_value = _mock_response(500, json_data={'message': 'Server error'})
        with self.assertRaises(GitHubAPIError) as ctx:
            GitHubClient(access_token='tok').get_authenticated_user()
        self.assertEqual(ctx.exception.status_code, 500)

    @patch('github_integration.services.github_client.requests.request')
    def test_non_json_error_body_falls_back_to_text(self, mock_request):
        response = _mock_response(502, text='<html>Bad Gateway</html>')
        response.json.side_effect = ValueError('not json')
        mock_request.return_value = response
        with self.assertRaises(GitHubAPIError) as ctx:
            GitHubClient(access_token='tok').get_authenticated_user()
        self.assertIn('Bad Gateway', str(ctx.exception))


class GitHubClientPaginationTests(SimpleTestCase):
    @patch('github_integration.services.github_client.requests.request')
    def test_follows_link_header_across_pages(self, mock_request):
        page_one = _mock_response(200, json_data=[{'id': 1}], links={'next': {'url': 'https://api.github.com/user/repos?page=2'}})
        page_two = _mock_response(200, json_data=[{'id': 2}], links={})
        mock_request.side_effect = [page_one, page_two]

        results = GitHubClient(access_token='tok').list_user_repositories()

        self.assertEqual(results, [{'id': 1}, {'id': 2}])
        self.assertEqual(mock_request.call_count, 2)

    @patch('github_integration.services.github_client.requests.request')
    def test_single_page_stops_after_one_request(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data=[{'id': 1}], links={})
        GitHubClient(access_token='tok').list_user_repositories()
        self.assertEqual(mock_request.call_count, 1)


class GitHubClientWebhookAndReviewTests(SimpleTestCase):
    @patch('github_integration.services.github_client.requests.request')
    def test_create_webhook_posts_expected_payload(self, mock_request):
        mock_request.return_value = _mock_response(201, json_data={'id': 555})
        result = GitHubClient(access_token='tok').create_webhook(
            'octocat', 'hello-world', 'https://example.com/api/webhooks/github/', 'shared-secret',
        )
        self.assertEqual(result, {'id': 555})
        _args, kwargs = mock_request.call_args
        self.assertEqual(kwargs['json']['events'], ['pull_request', 'push'])
        self.assertEqual(kwargs['json']['config']['secret'], 'shared-secret')

    @patch('github_integration.services.github_client.requests.request')
    def test_update_webhook_events_patches_events_only(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data={'id': 555, 'events': ['pull_request', 'push']})
        result = GitHubClient(access_token='tok').update_webhook_events(
            'octocat', 'hello-world', 555, ['pull_request', 'push'],
        )
        self.assertEqual(result['events'], ['pull_request', 'push'])
        args, kwargs = mock_request.call_args
        self.assertEqual(args[0], 'PATCH')
        self.assertTrue(args[1].endswith('/repos/octocat/hello-world/hooks/555'))
        self.assertEqual(kwargs['json'], {'events': ['pull_request', 'push']})

    @patch('github_integration.services.github_client.requests.request')
    def test_create_review_posts_comments_and_body(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data={'id': 9})
        GitHubClient(access_token='tok').create_review(
            'octocat', 'hello-world', 42, 'a' * 40, body='Looks good overall.',
            comments=[{'path': 'app.py', 'line': 3, 'side': 'RIGHT', 'body': 'issue here'}],
        )
        _args, kwargs = mock_request.call_args
        self.assertEqual(kwargs['json']['commit_id'], 'a' * 40)
        self.assertEqual(kwargs['json']['event'], 'COMMENT')
        self.assertEqual(len(kwargs['json']['comments']), 1)

    @patch('github_integration.services.github_client.requests.request')
    def test_get_file_content_decodes_base64(self, mock_request):
        import base64
        encoded = base64.b64encode(b'print("hello")').decode()
        mock_request.return_value = _mock_response(200, json_data={'encoding': 'base64', 'content': encoded})
        content = GitHubClient(access_token='tok').get_file_content('octocat', 'hello-world', 'app.py', 'main')
        self.assertEqual(content, 'print("hello")')

    @patch('github_integration.services.github_client.requests.request')
    def test_get_file_content_rejects_unexpected_encoding(self, mock_request):
        mock_request.return_value = _mock_response(200, json_data={'encoding': 'none', 'content': ''})
        with self.assertRaises(GitHubAPIError):
            GitHubClient(access_token='tok').get_file_content('octocat', 'hello-world', 'app.py', 'main')


@override_settings(
    GITHUB_CLIENT_ID='test-client-id', GITHUB_CLIENT_SECRET='test-secret',
    GITHUB_OAUTH_REDIRECT_URI='http://localhost:8000/api/github/callback/',
)
class ExchangeCodeForTokenTests(SimpleTestCase):
    @patch('github_integration.services.github_client.requests.post')
    def test_returns_token_data_on_success(self, mock_post):
        mock_post.return_value = _mock_response(200, json_data={'access_token': 'gho_abc', 'scope': 'repo'})
        result = GitHubClient.exchange_code_for_token('some-code')
        self.assertEqual(result['access_token'], 'gho_abc')

    @patch('github_integration.services.github_client.requests.post')
    def test_error_body_with_200_status_raises(self, mock_post):
        # GitHub returns HTTP 200 with an {"error": ...} body for a bad code.
        mock_post.return_value = _mock_response(200, json_data={'error': 'bad_verification_code'})
        with self.assertRaises(GitHubAPIError):
            GitHubClient.exchange_code_for_token('expired-code')

    @patch('github_integration.services.github_client.requests.post')
    def test_missing_access_token_in_response_raises(self, mock_post):
        mock_post.return_value = _mock_response(200, json_data={'scope': 'repo'})
        with self.assertRaises(GitHubAPIError):
            GitHubClient.exchange_code_for_token('some-code')
