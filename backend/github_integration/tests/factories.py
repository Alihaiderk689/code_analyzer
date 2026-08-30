"""Shared object builders for github_integration tests - kept in one place so
every test module builds a GitHubIntegration/GitHubRepository/PullRequestAnalysis
the same way, matching the make_authenticated_client/make_analysis pattern
already used in chat/tests.py.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from ..models import FileAnalysis, GitHubIntegration, GitHubRepository, PullRequestAnalysis, WebhookEvent

User = get_user_model()

TEST_ENCRYPTION_KEY = 'Okd60jRLH0kWT51ZnWHTjoWoiD5y-d-zP7mNtnbCmjY='


def make_user(email='ghuser@example.com', **overrides):
    defaults = dict(username=email.split('@')[0], email=email, password='TestPass123!')
    defaults.update(overrides)
    return User.objects.create_user(**defaults)


def make_authenticated_client(email='ghuser@example.com'):
    user = make_user(email)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def make_integration(user, github_user_id=1001, username='octocat', access_token='gho_rawtoken123', **overrides):
    integration = GitHubIntegration(user=user, github_user_id=github_user_id, username=username, **overrides)
    integration.set_access_token(access_token)
    integration.save()
    return integration


def make_repository(integration, repository_id=2001, full_name='octocat/hello-world', webhook_id=3001, **overrides):
    defaults = dict(repository_id=repository_id, full_name=full_name, webhook_id=webhook_id)
    defaults.update(overrides)
    return GitHubRepository.objects.create(integration=integration, **defaults)


def make_pr_analysis(repository, pull_request_number=1, commit_sha='a' * 40, **overrides):
    defaults = dict(pull_request_number=pull_request_number, commit_sha=commit_sha, title='Add feature', author='octocat')
    defaults.update(overrides)
    return PullRequestAnalysis.objects.create(repository=repository, **defaults)


def make_file_analysis(pr_analysis, file_path='app.py', issues=None, **overrides):
    defaults = dict(file_path=file_path, language='Python', issues=issues or [], score=90.0)
    defaults.update(overrides)
    return FileAnalysis.objects.create(pull_request_analysis=pr_analysis, **defaults)


def pull_request_webhook_payload(
    action='opened', repository_id=2001, repository_full_name='octocat/hello-world',
    pr_number=1, sha='a' * 40, title='Add feature', author='octocat',
):
    return {
        'action': action,
        'number': pr_number,
        'pull_request': {
            'number': pr_number, 'title': title, 'user': {'login': author}, 'head': {'sha': sha},
        },
        'repository': {'id': repository_id, 'full_name': repository_full_name},
    }


def push_webhook_payload(
    ref='refs/heads/main', deleted=False, repository_id=2001,
    repository_full_name='octocat/hello-world', default_branch='main',
):
    return {
        'ref': ref,
        'deleted': deleted,
        'repository': {'id': repository_id, 'full_name': repository_full_name, 'default_branch': default_branch},
    }


def make_webhook_event(delivery_id='d1', event_type='pull_request', payload=None, hook_id=None, **payload_overrides):
    if payload is None:
        payload = push_webhook_payload(**payload_overrides) if event_type == 'push' \
            else pull_request_webhook_payload(**payload_overrides)
    return WebhookEvent.objects.create(event_type=event_type, delivery_id=delivery_id, hook_id=hook_id, payload=payload)
