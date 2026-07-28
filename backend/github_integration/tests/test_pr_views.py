from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from ..models import PullRequestAnalysis
from .factories import (
    make_authenticated_client,
    make_file_analysis,
    make_integration,
    make_pr_analysis,
    make_repository,
    make_user,
)


def _set_created_at(pr_analysis, when):
    PullRequestAnalysis.objects.filter(pk=pr_analysis.pk).update(created_at=when)


class PullRequestAnalysisListViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-pull-requests'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_only_returns_the_users_own_pull_requests(self):
        client, user = make_authenticated_client()
        my_repo = make_repository(make_integration(user))
        make_pr_analysis(my_repo, pull_request_number=1)

        other_repo = make_repository(make_integration(make_user('other@example.com'), github_user_id=2))
        make_pr_analysis(other_repo, pull_request_number=1)

        response = client.get(reverse('github-pull-requests'))

        self.assertEqual(response.data['count'], 1)

    def test_filters_by_repository_id(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repo_a = make_repository(integration, repository_id=1, full_name='octocat/a')
        repo_b = make_repository(integration, repository_id=2, full_name='octocat/b')
        make_pr_analysis(repo_a, pull_request_number=1)
        make_pr_analysis(repo_b, pull_request_number=1)

        response = client.get(reverse('github-pull-requests'), {'repository_id': repo_a.id})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['repository_name'], 'octocat/a')

    def test_paginated_newest_first(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        for i in range(3):
            make_pr_analysis(repo, pull_request_number=i, commit_sha=str(i) * 40)

        response = client.get(reverse('github-pull-requests'))

        numbers = [r['pull_request_number'] for r in response.data['results']]
        self.assertEqual(numbers, [2, 1, 0])


class PullRequestAnalysisDetailViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-pull-request-detail', args=[1]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_includes_file_analyses(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        pr_analysis = make_pr_analysis(repo)
        make_file_analysis(pr_analysis, file_path='app.py', issues=[{'type': 'sql_injection', 'severity': 'high'}])

        response = client.get(reverse('github-pull-request-detail', args=[pr_analysis.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['file_analyses']), 1)
        self.assertEqual(response.data['file_analyses'][0]['file_path'], 'app.py')

    def test_404_for_another_users_pull_request(self):
        client, _user = make_authenticated_client()
        other_repo = make_repository(make_integration(make_user('other@example.com'), github_user_id=2))
        pr_analysis = make_pr_analysis(other_repo)

        response = client.get(reverse('github-pull-request-detail', args=[pr_analysis.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class DashboardMetricsViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-metrics'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty_state_returns_zeros_and_nulls(self):
        client, _user = make_authenticated_client()
        response = client.get(reverse('github-metrics'))
        self.assertEqual(response.data, {
            'total_prs_analyzed': 0, 'average_quality_score': None,
            'critical_vulnerabilities': 0, 'most_common_issue_type': None,
        })

    def test_computes_metrics_across_completed_analyses_only(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        completed = make_pr_analysis(
            repo, pull_request_number=1, status=PullRequestAnalysis.Status.COMPLETED, overall_score=80.0,
        )
        make_file_analysis(completed, issues=[
            {'type': 'sql_injection', 'severity': 'critical'},
            {'type': 'xss', 'severity': 'high'},
        ])
        pending = make_pr_analysis(
            repo, pull_request_number=2, commit_sha='b' * 40, status=PullRequestAnalysis.Status.PENDING,
        )
        make_file_analysis(pending, issues=[{'type': 'sql_injection', 'severity': 'critical'}])

        response = client.get(reverse('github-metrics'))

        self.assertEqual(response.data['total_prs_analyzed'], 1)
        self.assertEqual(response.data['average_quality_score'], 80.0)
        self.assertEqual(response.data['critical_vulnerabilities'], 1)
        self.assertEqual(response.data['most_common_issue_type'], 'sql_injection')

    def test_metrics_scoped_to_current_user(self):
        client, user = make_authenticated_client()
        make_repository(make_integration(user))
        other_repo = make_repository(make_integration(make_user('other@example.com'), github_user_id=2))
        make_pr_analysis(other_repo, status=PullRequestAnalysis.Status.COMPLETED, overall_score=10.0)

        response = client.get(reverse('github-metrics'))

        self.assertEqual(response.data['total_prs_analyzed'], 0)


class QualityTrendsViewTests(TestCase):
    def test_requires_authentication(self):
        response = APIClient().get(reverse('github-pull-requests-trends'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty_state_returns_empty_results(self):
        client, _user = make_authenticated_client()
        response = client.get(reverse('github-pull-requests-trends'))
        self.assertEqual(response.data, {'results': []})

    def test_groups_by_day_and_averages_scores(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        now = timezone.now()

        first = make_pr_analysis(repo, pull_request_number=1, status=PullRequestAnalysis.Status.COMPLETED, overall_score=80.0)
        _set_created_at(first, now)
        second = make_pr_analysis(
            repo, pull_request_number=2, commit_sha='b' * 40, status=PullRequestAnalysis.Status.COMPLETED, overall_score=60.0,
        )
        _set_created_at(second, now)

        response = client.get(reverse('github-pull-requests-trends'))

        self.assertEqual(len(response.data['results']), 1)
        point = response.data['results'][0]
        self.assertEqual(point['average_score'], 70.0)
        self.assertEqual(point['prs_count'], 2)
        self.assertEqual(point['date'], now.date().isoformat())

    def test_excludes_non_completed_and_null_score_analyses(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        make_pr_analysis(repo, pull_request_number=1, status=PullRequestAnalysis.Status.PENDING, overall_score=None)
        make_pr_analysis(
            repo, pull_request_number=2, commit_sha='b' * 40, status=PullRequestAnalysis.Status.COMPLETED, overall_score=None,
        )

        response = client.get(reverse('github-pull-requests-trends'))

        self.assertEqual(response.data['results'], [])

    def test_excludes_analyses_outside_the_days_window(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        old = make_pr_analysis(repo, pull_request_number=1, status=PullRequestAnalysis.Status.COMPLETED, overall_score=50.0)
        _set_created_at(old, timezone.now() - timezone.timedelta(days=40))

        response = client.get(reverse('github-pull-requests-trends'), {'days': 30})

        self.assertEqual(response.data['results'], [])

    def test_filters_by_repository_id(self):
        client, user = make_authenticated_client()
        integration = make_integration(user)
        repo_a = make_repository(integration, repository_id=1, full_name='octocat/a')
        repo_b = make_repository(integration, repository_id=2, full_name='octocat/b')
        make_pr_analysis(repo_a, pull_request_number=1, status=PullRequestAnalysis.Status.COMPLETED, overall_score=90.0)
        make_pr_analysis(repo_b, pull_request_number=1, status=PullRequestAnalysis.Status.COMPLETED, overall_score=10.0)

        response = client.get(reverse('github-pull-requests-trends'), {'repository_id': repo_a.id})

        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['average_score'], 90.0)

    def test_scoped_to_current_user(self):
        client, user = make_authenticated_client()
        make_repository(make_integration(user))
        other_repo = make_repository(make_integration(make_user('other@example.com'), github_user_id=2))
        make_pr_analysis(other_repo, status=PullRequestAnalysis.Status.COMPLETED, overall_score=10.0)

        response = client.get(reverse('github-pull-requests-trends'))

        self.assertEqual(response.data['results'], [])

    def test_invalid_days_param_falls_back_to_default(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        pr = make_pr_analysis(repo, status=PullRequestAnalysis.Status.COMPLETED, overall_score=50.0)
        _set_created_at(pr, timezone.now())

        response = client.get(reverse('github-pull-requests-trends'), {'days': 'not-a-number'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_days_param_clamped_to_max(self):
        client, user = make_authenticated_client()
        repo = make_repository(make_integration(user))
        pr = make_pr_analysis(repo, status=PullRequestAnalysis.Status.COMPLETED, overall_score=50.0)
        _set_created_at(pr, timezone.now())

        response = client.get(reverse('github-pull-requests-trends'), {'days': 999999})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
