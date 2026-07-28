"""Thin wrapper around GitHub's REST API using `requests` (already a project
dependency) rather than a full SDK like PyGithub - the surface area this
feature needs (OAuth exchange, list repos, manage a webhook, list PR files,
post a review) is a handful of straightforward JSON endpoints, and a bespoke
SDK dependency isn't worth it for that.

Every method raises GitHubAPIError (or the GitHubRateLimitError subclass) on
failure rather than returning None/False, so callers (views, Celery tasks)
have one place to handle "GitHub is unhappy" - see raise_for_github_response().
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = 'https://api.github.com/'
GITHUB_OAUTH_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
GITHUB_OAUTH_TOKEN_URL = 'https://github.com/login/oauth/access_token'
REQUEST_TIMEOUT_SECONDS = 15


class GitHubAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitHubAuthError(GitHubAPIError):
    """401 - the stored access token is invalid/revoked. Callers should mark
    the GitHubIntegration as needing reconnect rather than retrying."""


class GitHubRateLimitError(GitHubAPIError):
    """403/429 with rate-limit headers exhausted. Carries `reset_at` (unix
    timestamp) so callers (Celery tasks) can retry at exactly the right time
    instead of guessing a backoff."""

    def __init__(self, message: str, reset_at: Optional[int], status_code: Optional[int] = None):
        super().__init__(message, status_code=status_code)
        self.reset_at = reset_at


def build_authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    params = {
        'client_id': settings.GITHUB_CLIENT_ID,
        'redirect_uri': settings.GITHUB_OAUTH_REDIRECT_URI,
        'scope': 'repo admin:repo_hook read:user user:email',
        'state': state,
        'allow_signup': 'true',
    }
    return f'{GITHUB_OAUTH_AUTHORIZE_URL}?{urlencode(params)}'


class GitHubClient:
    def __init__(self, access_token: Optional[str] = None):
        self.access_token = access_token

    # ---- low-level request plumbing -------------------------------------------------

    def _headers(self) -> dict:
        headers = {
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        if self.access_token:
            headers['Authorization'] = f'Bearer {self.access_token}'
        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith('http') else urljoin(GITHUB_API_BASE, path.lstrip('/'))
        try:
            response = requests.request(
                method, url, headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS, **kwargs,
            )
        except requests.RequestException as exc:
            logger.error(
                'github_api.network_error', extra={'method': method, 'url': url, 'error': str(exc)},
            )
            raise GitHubAPIError(f'Network error calling GitHub API: {exc}') from exc

        self._raise_for_response(response, method, url)
        return response

    @staticmethod
    def _raise_for_response(response: requests.Response, method: str, url: str) -> None:
        if response.ok:
            return

        try:
            body = response.json()
        except ValueError:
            body = response.text

        logger.warning(
            'github_api.error_response',
            extra={'method': method, 'url': url, 'status_code': response.status_code, 'body': body},
        )

        if response.status_code == 401:
            raise GitHubAuthError('GitHub access token is invalid or has been revoked.', 401, body)

        remaining = response.headers.get('X-RateLimit-Remaining')
        if response.status_code in (403, 429) and remaining == '0':
            reset_at = response.headers.get('X-RateLimit-Reset')
            raise GitHubRateLimitError(
                'GitHub API rate limit exceeded.', reset_at=int(reset_at) if reset_at else None,
                status_code=response.status_code,
            )

        message = body.get('message') if isinstance(body, dict) else str(body)
        raise GitHubAPIError(
            f'GitHub API request failed ({response.status_code}): {message}',
            status_code=response.status_code, response_body=body,
        )

    def _paginated(self, path: str, params: Optional[dict] = None) -> list[dict]:
        """Follows the RFC 5988 `Link: <...>; rel="next"` header GitHub uses
        for pagination, rather than guessing page counts up front."""
        results: list[dict] = []
        url = urljoin(GITHUB_API_BASE, path.lstrip('/'))
        request_params = {**(params or {}), 'per_page': 100}

        while url:
            response = self._request('GET', url, params=request_params)
            data = response.json()
            results.extend(data if isinstance(data, list) else data.get('items', []))
            url = response.links.get('next', {}).get('url')
            request_params = None  # already encoded into the `next` URL
        return results

    # ---- OAuth ------------------------------------------------------------------------

    @staticmethod
    def exchange_code_for_token(code: str) -> dict:
        response = requests.post(
            GITHUB_OAUTH_TOKEN_URL,
            headers={'Accept': 'application/json'},
            data={
                'client_id': settings.GITHUB_CLIENT_ID,
                'client_secret': settings.GITHUB_CLIENT_SECRET,
                'code': code,
                'redirect_uri': settings.GITHUB_OAUTH_REDIRECT_URI,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        GitHubClient._raise_for_response(response, 'POST', GITHUB_OAUTH_TOKEN_URL)
        data = response.json()
        if 'error' in data:
            # GitHub returns 200 with an {"error": ...} body for a bad/expired
            # code, rather than a non-2xx status - has to be checked separately.
            raise GitHubAPIError(f"GitHub OAuth error: {data.get('error_description', data['error'])}")
        if 'access_token' not in data:
            raise GitHubAPIError('GitHub OAuth response did not include an access_token.')
        return data

    def get_authenticated_user(self) -> dict:
        return self._request('GET', '/user').json()

    # ---- Repositories -------------------------------------------------------------------

    def list_user_repositories(self) -> list[dict]:
        return self._paginated('/user/repos', params={'sort': 'updated', 'affiliation': 'owner,collaborator'})

    def create_webhook(self, owner: str, repo: str, webhook_url: str, secret: str) -> dict:
        return self._request(
            'POST', f'/repos/{owner}/{repo}/hooks',
            json={
                'name': 'web',
                'active': True,
                'events': ['pull_request'],
                'config': {'url': webhook_url, 'content_type': 'json', 'secret': secret, 'insecure_ssl': '0'},
            },
        ).json()

    def delete_webhook(self, owner: str, repo: str, hook_id: int) -> None:
        self._request('DELETE', f'/repos/{owner}/{repo}/hooks/{hook_id}')

    # ---- Pull requests ------------------------------------------------------------------

    def list_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return self._paginated(f'/repos/{owner}/{repo}/pulls/{pr_number}/files')

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        import base64
        data = self._request('GET', f'/repos/{owner}/{repo}/contents/{path}', params={'ref': ref}).json()
        if data.get('encoding') != 'base64':
            raise GitHubAPIError(f'Unexpected content encoding for {path}: {data.get("encoding")}')
        return base64.b64decode(data['content']).decode('utf-8', errors='replace')

    def get_repository_tree(self, owner: str, repo: str, ref: str) -> list[dict]:
        """One request for the *entire* file tree (recursive=1), not one call
        per folder - browsing a repo must not cost more than a couple of API
        calls total, regardless of how many files/directories it has."""
        data = self._request(
            'GET', f'/repos/{owner}/{repo}/git/trees/{ref}', params={'recursive': '1'},
        ).json()
        if data.get('truncated'):
            logger.warning('github_client.tree_truncated', extra={'repo': f'{owner}/{repo}'})
        return [
            {'path': item['path'], 'type': 'dir' if item['type'] == 'tree' else 'file', 'size': item.get('size')}
            for item in data.get('tree', [])
            if item['type'] in ('blob', 'tree')
        ]

    def create_review(
        self, owner: str, repo: str, pr_number: int, commit_id: str, body: str,
        comments: Optional[list[dict]] = None, event: str = 'COMMENT',
    ) -> dict:
        payload = {'commit_id': commit_id, 'body': body, 'event': event, 'comments': comments or []}
        return self._request('POST', f'/repos/{owner}/{repo}/pulls/{pr_number}/reviews', json=payload).json()
