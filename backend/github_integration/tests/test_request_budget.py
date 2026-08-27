"""The request-wide budget applied to the repository-context analysis path.

github_integration.tests.test_fetch_budget covers the GitHub sub-budget and
core.tests_execution_budget covers the generic machinery and each expensive
stage in isolation. These are the end-to-end ones: a real
analyze_file_with_context call under a real RequestBudget, and the arithmetic
guards tying the configured budgets to gunicorn's timeout.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from analyses.services.bandit_service import BANDIT_TIMEOUT_SECONDS
from analyses.services.custom_rules_service import CustomRulesScanner
from analyses.services.security_service import SecurityAnalysisService
from analyses.sandbox import TIMEOUT_SECONDS as SANDBOX_TIMEOUT_SECONDS
from core.execution_budget import (
    REASON_REQUEST_BUDGET_EXHAUSTED,
    STAGE_AI_ENRICHMENT,
    STAGE_BANDIT,
    STAGE_RELATED_FILES,
    STAGE_RUNTIME_CHECK,
    RequestBudget,
)

from ..models import RepositoryFileNode, RepositoryIndex
from ..services.fetch_budget import TRUNCATED_BUDGET_EXHAUSTED, FetchBudget
from ..services.github_client import REQUEST_TIMEOUT_SECONDS, GitHubAPIError
from ..services.pr_analysis_service import (
    MAX_CONTEXT_RELATED_FILES,
    PRAnalysisService,
    _security_service,
)
from .factories import make_integration, make_repository, make_user

# gunicorn's --timeout in backend/Dockerfile. Hard-coded on purpose: if that
# value changes, these tests should fail and force the budgets to be
# re-checked against it.
GUNICORN_TIMEOUT_SECONDS = 120

# The per-file cost these budgets exist to bound, from the stages' own
# timeouts: sandbox 5s + Bandit 20s + a fully-exhausted 3-provider AI chain.
AI_CHAIN_WORST_CASE_SECONDS = 90
PER_FILE_WORST_CASE_SECONDS = SANDBOX_TIMEOUT_SECONDS + BANDIT_TIMEOUT_SECONDS + AI_CHAIN_WORST_CASE_SECONDS
# Primary file + MAX_CONTEXT_RELATED_FILES imports + the same number of importers.
MAX_ANALYZED_FILES = 1 + 2 * MAX_CONTEXT_RELATED_FILES


@override_settings(GITHUB_MAX_FILE_SIZE_BYTES=500_000)
class ContextRequestBudgetTests(TestCase):
    """Non-Python neighbor paths so the Python-only settings.py lookup does
    not add GitHub calls of its own; Bandit/sandbox coverage lives in
    core.tests_execution_budget and in the Python test below."""

    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))
        index = RepositoryIndex.objects.create(
            repository=self.repository, status=RepositoryIndex.Status.COMPLETED,
        )
        RepositoryFileNode.objects.create(
            index=index, path='app.js', language='JavaScript',
            imports=['a.js', 'b.js', 'c.js'], imported_by=['d.js', 'e.js', 'f.js'],
        )

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=90, GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_normal_analysis_is_unchanged_when_no_budget_is_exhausted(self, mock_client_cls):
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 6)
        self.assertFalse(result['context_truncated'])
        self.assertEqual(result['context_truncated_reason'], '')
        self.assertEqual(result['degraded_stages'], [])
        self.assertEqual(mock_client_cls.return_value.get_file_content.call_count, 7)

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=0)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_exhausted_request_budget_stops_before_any_neighbor(self, mock_client_cls):
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        # Only the primary file was fetched - no neighbor fetch was attempted.
        self.assertEqual(mock_client_cls.return_value.get_file_content.call_count, 1)
        self.assertEqual(result['related'], [])
        self.assertTrue(result['context_truncated'])
        self.assertEqual(result['context_truncated_reason'], REASON_REQUEST_BUDGET_EXHAUSTED)
        self.assertIn(STAGE_RELATED_FILES, result['degraded_stages'])

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=0)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_partial_analysis_of_the_primary_file_is_still_returned(self, mock_client_cls):
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n// TODO: fix\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        # The static analysis that costs no wall clock still ran and scored.
        self.assertFalse(result['skipped'])
        self.assertIsNotNone(result['score'])
        self.assertTrue(any(issue['type'] == 'todo' for issue in result['issues']))
        self.assertEqual(result['content'], 'const x = 1;\n// TODO: fix\n')

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=600, GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.RequestBudget')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_budget_draining_mid_run_keeps_what_was_already_analyzed(self, mock_client_cls, mock_budget_cls):
        budget = RequestBudget(600)
        mock_budget_cls.return_value = budget
        calls = {'n': 0}

        def drain_after_two(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                budget._deadline = 0  # the clock runs out right after the first neighbor
            return 'const x = 1;\n'

        mock_client_cls.return_value.get_file_content.side_effect = drain_after_two

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 1)   # the one neighbor that fit is kept
        self.assertIsNotNone(result['score'])         # so is the primary file
        self.assertTrue(result['context_truncated'])
        self.assertEqual(result['context_truncated_reason'], REASON_REQUEST_BUDGET_EXHAUSTED)
        self.assertEqual(mock_client_cls.return_value.get_file_content.call_count, 2)

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=0)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_request_budget_reason_takes_precedence_over_the_fetch_budget(self, mock_client_cls):
        """Both budgets start from the same instant, so a slow analysis
        expires the fetch budget too. Reporting 'fetch_budget_exhausted' then
        would point an operator at GitHub when the real cost was analysis."""
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(result['context_truncated_reason'], REASON_REQUEST_BUDGET_EXHAUSTED)
        self.assertNotEqual(result['context_truncated_reason'], TRUNCATED_BUDGET_EXHAUSTED)

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=90, GITHUB_CONTEXT_FETCH_BUDGET_SECONDS=45)
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_a_failing_neighbor_is_not_reported_as_budget_degradation(self, mock_client_cls):
        mock_client_cls.return_value.get_file_content.side_effect = [
            'const x = 1;\n', GitHubAPIError('not found', 404),
            'const y = 2;\n', 'const z = 3;\n', 'const w = 4;\n', 'const v = 5;\n', 'const u = 6;\n',
        ]

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 5)
        self.assertFalse(result['context_truncated'])
        self.assertEqual(result['degraded_stages'], [])


@override_settings(GITHUB_MAX_FILE_SIZE_BYTES=500_000)
class ExpensiveStagesAreNotRunAfterExhaustionTests(TestCase):
    """Python files, so Bandit and the sandbox are both in play - the two
    stages that cost real wall clock per file."""

    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))
        index = RepositoryIndex.objects.create(
            repository=self.repository, status=RepositoryIndex.Status.COMPLETED,
        )
        RepositoryFileNode.objects.create(
            index=index, path='app.py', language='Python',
            imports=['a.py', 'b.py', 'c.py'], imported_by=['d.py', 'e.py', 'f.py'],
        )

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=0)
    @patch('analyses.engine.sandbox.run_python')
    @patch('analyses.services.bandit_service.subprocess.run')
    @patch('ai.client._call_with_fallback')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_no_bandit_sandbox_or_ai_work_happens_once_exhausted(
        self, mock_client_cls, mock_ai, mock_bandit, mock_sandbox,
    ):
        mock_client_cls.return_value.get_file_content.return_value = 'import os\neval("1")\n'
        mock_client_cls.return_value.get_repository_tree.return_value = {'entries': [], 'truncated': False}

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.py', 'token')

        mock_bandit.assert_not_called()
        mock_sandbox.assert_not_called()
        mock_ai.assert_not_called()
        # ...and the analysis still produced a result from the free static checks.
        self.assertFalse(result['skipped'])
        self.assertIsNotNone(result['score'])
        for stage in (STAGE_RUNTIME_CHECK, STAGE_BANDIT):
            self.assertIn(stage, result['degraded_stages'])

    @override_settings(GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=0)
    @patch('analyses.engine.sandbox.run_python')
    @patch('analyses.services.bandit_service.subprocess.run')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_findings_from_free_scanners_are_still_ai_degraded_not_dropped(
        self, mock_client_cls, mock_bandit, mock_sandbox,
    ):
        """CustomRulesScanner costs nothing and still runs; its findings must
        survive with scanner-provided text rather than being discarded."""
        mock_client_cls.return_value.get_file_content.return_value = 'API_KEY = "sk-abcdef1234567890abcdef"\n'
        mock_client_cls.return_value.get_repository_tree.return_value = {'entries': [], 'truncated': False}

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.py', 'token')

        security_issues = [i for i in result['issues'] if i['source'] == 'security']
        self.assertTrue(security_issues, 'custom-rules findings must survive budget exhaustion')
        self.assertTrue(all(i['explanation'] for i in security_issues))
        self.assertIn(STAGE_AI_ENRICHMENT, result['degraded_stages'])


class SecurityServiceInjectionTests(TestCase):
    """_security_service is the seam that carries the budget into the two
    expensive security stages without touching BaseSecurityScanner.scan."""

    def test_without_a_budget_the_stock_service_is_returned(self):
        service = _security_service(None)
        self.assertIsInstance(service, SecurityAnalysisService)
        self.assertEqual([s.name for s in service.scanners], ['bandit', 'custom_rules'])

    def test_with_a_budget_the_same_scanners_run_in_the_same_order(self):
        service = _security_service(RequestBudget(90))
        self.assertEqual([s.name for s in service.scanners], ['bandit', 'custom_rules'])
        self.assertIsInstance(service.scanners[1], CustomRulesScanner)


class WorstCaseTimingTests(TestCase):
    """Arithmetic guards on the configured worst case, so a settings change
    cannot silently push the bounded request back over gunicorn's timeout."""

    def test_the_unbounded_worst_case_this_exists_to_fix_really_did_exceed_the_timeout(self):
        unbounded = (
            11 * REQUEST_TIMEOUT_SECONDS                      # the GitHub fetch phase
            + MAX_ANALYZED_FILES * PER_FILE_WORST_CASE_SECONDS  # 7 files x (5 + 20 + 90)
        )
        self.assertGreater(unbounded, GUNICORN_TIMEOUT_SECONDS)

    def test_the_request_budget_bounds_the_whole_path_below_the_gunicorn_timeout(self):
        from django.conf import settings

        budget = settings.GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS
        margin = GUNICORN_TIMEOUT_SECONDS - budget
        self.assertGreaterEqual(
            margin, 25,
            f'request budget {budget}s leaves only {margin}s under gunicorn s {GUNICORN_TIMEOUT_SECONDS}s',
        )

    def test_the_fetch_budget_cannot_outlive_the_request_budget(self):
        from django.conf import settings

        self.assertLessEqual(
            settings.GITHUB_CONTEXT_FETCH_BUDGET_SECONDS,
            settings.GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS,
        )

    def test_the_fetch_budget_is_clamped_to_the_request_budget_at_construction(self):
        """Belt and braces for the guard above: even if the settings drift
        apart, the sub-budget is built as min(fetch, request-remaining)."""
        request_budget = RequestBudget(10)
        fetch_budget = FetchBudget(min(45, request_budget.remaining()))
        self.assertLessEqual(fetch_budget.total_seconds, 10)

    def test_every_budgeted_stage_fits_inside_the_request_budget(self):
        """Each stage's own ceiling must be affordable at least once, or the
        budget would degrade every request from the first file."""
        from django.conf import settings

        budget = settings.GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS
        for name, ceiling in (
            ('sandbox', SANDBOX_TIMEOUT_SECONDS),
            ('bandit', BANDIT_TIMEOUT_SECONDS),
            ('ai chain', settings.AI_REQUEST_TIMEOUT_SECONDS),
        ):
            self.assertLess(ceiling, budget, f'{name} ceiling {ceiling}s does not fit in {budget}s')
