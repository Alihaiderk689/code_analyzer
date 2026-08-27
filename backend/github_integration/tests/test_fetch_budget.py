"""The total repository-context fetch budget (services/fetch_budget.py).

Per-request timeouts bound each GitHub call in isolation; these cover the
thing they cannot express - the sum. The context path makes up to 11 fetches,
which at github_client.REQUEST_TIMEOUT_SECONDS each is 165s worst case,
against gunicorn's --timeout 120.
"""
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from ..models import RepositoryFileNode, RepositoryIndex
from ..services.fetch_budget import (
    TRUNCATED_BUDGET_EXHAUSTED,
    FetchBudget,
    FetchBudgetExceeded,
)
from ..services.github_client import REQUEST_TIMEOUT_SECONDS, GitHubAPIError, GitHubClient
from ..services.pr_analysis_service import PRAnalysisService
from .factories import make_integration, make_repository, make_user

# gunicorn's --timeout in backend/Dockerfile. Hard-coded here on purpose: if
# that value changes, this test should fail and force the budgets to be
# re-checked against it.
GUNICORN_TIMEOUT_SECONDS = 120


class FetchBudgetTests(TestCase):
    def test_slice_is_clamped_to_what_remains(self):
        with patch('core.execution_budget.time.monotonic', side_effect=[0, 2, 2]):
            budget = FetchBudget(4)  # deadline = 0 + 4
            self.assertEqual(budget.slice_for(REQUEST_TIMEOUT_SECONDS, stage='t'), 2)

    def test_slice_is_the_per_request_timeout_while_budget_is_plentiful(self):
        budget = FetchBudget(600)
        self.assertEqual(budget.slice_for(REQUEST_TIMEOUT_SECONDS, stage='t'), REQUEST_TIMEOUT_SECONDS)

    def test_exhausted_budget_raises_and_is_not_a_github_error(self):
        budget = FetchBudget(0)
        with self.assertRaises(FetchBudgetExceeded):
            budget.slice_for(REQUEST_TIMEOUT_SECONDS, stage='t')
        self.assertTrue(budget.exhausted)
        # The distinction requirement 3 turns on: every caller's
        # `except GitHubAPIError` must NOT catch this.
        self.assertFalse(issubclass(FetchBudgetExceeded, GitHubAPIError))


class GitHubClientBudgetTests(TestCase):
    """The client is where the budget becomes a real bound rather than a
    pre-flight check: each call's timeout is clamped to what's left."""

    @patch('github_integration.services.github_client.requests.request')
    def test_unbudgeted_client_still_uses_the_full_per_request_timeout(self, mock_request):
        mock_request.return_value.ok = True
        mock_request.return_value.json.return_value = {}

        GitHubClient('token')._request('GET', '/user')

        self.assertEqual(mock_request.call_args.kwargs['timeout'], REQUEST_TIMEOUT_SECONDS)

    @patch('github_integration.services.github_client.requests.request')
    def test_budgeted_client_clamps_the_timeout_to_the_remaining_budget(self, mock_request):
        mock_request.return_value.ok = True
        mock_request.return_value.json.return_value = {}

        GitHubClient('token', budget=FetchBudget(5))._request('GET', '/user')

        self.assertLessEqual(mock_request.call_args.kwargs['timeout'], 5)

    @patch('github_integration.services.github_client.requests.request')
    def test_no_request_is_made_once_the_budget_is_spent(self, mock_request):
        with self.assertRaises(FetchBudgetExceeded):
            GitHubClient('token', budget=FetchBudget(0))._request('GET', '/user')
        mock_request.assert_not_called()

    @patch('github_integration.services.github_client.requests.request')
    def test_timing_out_on_the_last_slice_reports_budget_not_network_failure(self, mock_request):
        mock_request.side_effect = requests.Timeout('read timeout')
        budget = FetchBudget(0.001)

        with self.assertRaises(FetchBudgetExceeded):
            GitHubClient('token', budget=budget)._request('GET', '/user')

    @patch('github_integration.services.github_client.requests.request')
    def test_genuine_network_failure_with_budget_left_is_still_a_github_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError('dns')
        budget = FetchBudget(600)

        with self.assertRaises(GitHubAPIError):
            GitHubClient('token', budget=budget)._request('GET', '/user')
        self.assertFalse(budget.exhausted)


@override_settings(GITHUB_MAX_FILE_SIZE_BYTES=500_000)
class ContextAnalysisBudgetTests(TestCase):
    """analyze_file_with_context under a budget. Non-Python paths throughout
    so the Python-only settings.py lookup never adds fetches of its own."""

    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))
        index = RepositoryIndex.objects.create(
            repository=self.repository, status=RepositoryIndex.Status.COMPLETED,
        )
        RepositoryFileNode.objects.create(
            index=index, path='app.js', language='JavaScript',
            imports=['a.js', 'b.js', 'c.js'], imported_by=['d.js', 'e.js', 'f.js'],
        )

    @override_settings(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_normal_analysis_is_unchanged_when_the_budget_is_not_exhausted(self, mock_client_cls):
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 6)  # 3 imports + 3 importers, the pre-existing cap
        self.assertFalse(result['context_truncated'])
        self.assertEqual(result['context_truncated_reason'], '')
        self.assertEqual(mock_client_cls.return_value.get_file_content.call_count, 7)  # primary + 6

    @override_settings(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.FetchBudget')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_exhausted_budget_stops_further_fetches(self, mock_client_cls, mock_budget_cls):
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'
        # Budget survives the primary file, then reports spent before the
        # second neighbor.
        mock_budget_cls.return_value.expired.side_effect = [False, True]
        mock_budget_cls.return_value.exhausted = True

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        # primary + exactly one neighbor: the remaining five were never fetched.
        self.assertEqual(mock_client_cls.return_value.get_file_content.call_count, 2)
        self.assertEqual(len(result['related']), 1)

    @override_settings(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.FetchBudget')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_partial_context_still_proceeds_to_analysis(self, mock_client_cls, mock_budget_cls):
        mock_client_cls.return_value.get_file_content.side_effect = [
            'const x = 1;\n',          # primary
            'eval(userInput);\n',      # neighbor a.js - analyzed
        ]
        mock_budget_cls.return_value.expired.side_effect = [False, True]
        mock_budget_cls.return_value.exhausted = True

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertFalse(result['skipped'])
        self.assertIsNotNone(result['score'])          # primary was fully analyzed
        self.assertEqual(len(result['related']), 1)
        self.assertIn('issues', result['related'][0])  # the partial neighbor went through the pipeline
        self.assertTrue(result['context_truncated'])
        self.assertEqual(result['context_truncated_reason'], TRUNCATED_BUDGET_EXHAUSTED)

    @override_settings(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.FetchBudget')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_budget_exceeded_mid_fetch_keeps_what_was_collected(self, mock_client_cls, mock_budget_cls):
        mock_client_cls.return_value.get_file_content.side_effect = [
            'const x = 1;\n',
            'const y = 2;\n',
            FetchBudgetExceeded('out of time'),
        ]
        mock_budget_cls.return_value.expired.return_value = False
        mock_budget_cls.return_value.exhausted = True

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 1)
        self.assertTrue(result['context_truncated'])

    @override_settings(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_a_failing_neighbor_is_not_reported_as_truncation(self, mock_client_cls):
        """Requirement 3's distinction: a 404/permissions failure on one
        neighbor is a per-file failure, not an exhausted budget."""
        mock_client_cls.return_value.get_file_content.side_effect = [
            'const x = 1;\n', GitHubAPIError('not found', 404),
            'const y = 2;\n', 'const z = 3;\n', 'const w = 4;\n', 'const v = 5;\n', 'const u = 6;\n',
        ]

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 5)  # 6 neighbors, one unfetchable
        self.assertFalse(result['context_truncated'])
        self.assertEqual(result['context_truncated_reason'], '')


class WorstCaseTimingTests(TestCase):
    """Arithmetic guards on the configured worst case, so a future settings
    change can't silently push the bounded part of the request back over
    gunicorn's timeout."""

    def test_context_fetch_phase_is_bounded_well_below_gunicorn_timeout(self):
        from django.conf import settings

        budget = settings.GITHUB_CONTEXT_FETCH_BUDGET_SECONDS
        # Before this change the same phase's worst case was 11 x 15 = 165s.
        self.assertLess(budget, 11 * REQUEST_TIMEOUT_SECONDS)
        self.assertLessEqual(budget, GUNICORN_TIMEOUT_SECONDS / 2)

    def test_the_fetch_phase_is_a_share_of_the_request_budget_not_an_addition(self):
        """The fetch budget is a sub-budget: it is constructed as
        min(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS, request_budget.remaining()),
        so the two are never summed against gunicorn's timeout. The overall
        bound is asserted in tests.test_request_budget.WorstCaseTimingTests;
        Bandit and the AI chain are bounded there too, not here."""
        from django.conf import settings

        self.assertLessEqual(
            settings.GITHUB_CONTEXT_FETCH_BUDGET_SECONDS,
            settings.GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS,
        )
        self.assertLess(settings.GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS, GUNICORN_TIMEOUT_SECONDS)
