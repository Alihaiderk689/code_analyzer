"""The request-wide execution budget (core/execution_budget.py).

Companion to github_integration.tests.test_fetch_budget, which covers the
GitHub-fetch sub-budget. These cover the generic machinery and the three
expensive stages it gates: the sandboxed runtime check, Bandit, and the AI
provider fallback chain.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.execution_budget import (
    STAGE_AI_ENRICHMENT,
    STAGE_BANDIT,
    STAGE_RUNTIME_CHECK,
    BudgetExceeded,
    ExecutionBudget,
    RequestBudget,
)


class ExecutionBudgetTests(TestCase):
    def test_remaining_never_goes_negative(self):
        self.assertEqual(ExecutionBudget(0).remaining(), 0.0)

    def test_slice_is_clamped_to_what_remains(self):
        with patch('core.execution_budget.time.monotonic', side_effect=[0, 3, 3]):
            budget = ExecutionBudget(10)  # deadline = 0 + 10
            self.assertEqual(budget.slice_for(30, stage='s'), 7)

    def test_slice_is_the_default_while_the_budget_is_plentiful(self):
        self.assertEqual(ExecutionBudget(600).slice_for(30, stage='s'), 30)

    def test_slice_raises_once_spent(self):
        budget = ExecutionBudget(0)
        with self.assertRaises(BudgetExceeded):
            budget.slice_for(30, stage='s')
        self.assertTrue(budget.exhausted)

    def test_can_afford_records_the_skipped_stage_and_exhaustion(self):
        budget = ExecutionBudget(1)
        self.assertFalse(budget.can_afford(20, STAGE_BANDIT))
        self.assertEqual(budget.degraded_stages, [STAGE_BANDIT])
        self.assertTrue(budget.exhausted)

    def test_can_afford_leaves_a_plentiful_budget_untouched(self):
        budget = ExecutionBudget(600)
        self.assertTrue(budget.can_afford(20, STAGE_BANDIT))
        self.assertEqual(budget.degraded_stages, [])
        self.assertFalse(budget.exhausted)

    def test_the_same_stage_is_recorded_once(self):
        budget = ExecutionBudget(0)
        budget.can_afford(20, STAGE_BANDIT)
        budget.can_afford(20, STAGE_BANDIT)
        budget.can_afford(8, STAGE_AI_ENRICHMENT)
        self.assertEqual(budget.degraded_stages, [STAGE_BANDIT, STAGE_AI_ENRICHMENT])

    def test_degraded_stages_is_a_copy_callers_cannot_mutate(self):
        budget = ExecutionBudget(0)
        budget.mark_skipped(STAGE_BANDIT)
        budget.degraded_stages.append('nonsense')
        self.assertEqual(budget.degraded_stages, [STAGE_BANDIT])


class AIChainBudgetTests(TestCase):
    """ai.client._call_with_fallback under a budget. The fallback ORDER and
    the per-provider failure handling must be untouched - only the decision to
    keep walking the chain is budgeted."""

    @override_settings(AI_REQUEST_TIMEOUT_SECONDS=30)
    @patch('ai.client._call_groq')
    def test_unbudgeted_call_passes_no_timeout_override(self, mock_groq):
        from ai.client import _call_with_fallback

        mock_groq.return_value = 'ok'
        self.assertEqual(_call_with_fallback([{'role': 'user', 'content': 'hi'}]), 'ok')
        self.assertIsNone(mock_groq.call_args.kwargs['timeout'])

    @override_settings(AI_REQUEST_TIMEOUT_SECONDS=30)
    @patch('ai.client._call_groq')
    def test_budgeted_call_clamps_the_provider_timeout(self, mock_groq):
        from ai.client import _call_with_fallback

        mock_groq.return_value = 'ok'
        _call_with_fallback([{'role': 'user', 'content': 'hi'}], budget=RequestBudget(12))
        self.assertLessEqual(mock_groq.call_args.kwargs['timeout'], 12)

    @override_settings(AI_REQUEST_TIMEOUT_SECONDS=30)
    @patch('ai.client._call_openrouter')
    @patch('ai.client._call_gemini')
    @patch('ai.client._call_groq')
    def test_fallback_order_is_preserved_under_a_healthy_budget(self, mock_groq, mock_gemini, mock_or):
        from ai.client import _call_with_fallback

        mock_groq.side_effect = RuntimeError('groq down')
        mock_gemini.side_effect = RuntimeError('gemini down')
        mock_or.return_value = 'from openrouter'

        result = _call_with_fallback([{'role': 'user', 'content': 'hi'}], budget=RequestBudget(600))

        self.assertEqual(result, 'from openrouter')
        mock_groq.assert_called_once()
        mock_gemini.assert_called_once()

    @override_settings(AI_REQUEST_TIMEOUT_SECONDS=30)
    @patch('ai.client._call_openrouter')
    @patch('ai.client._call_gemini')
    @patch('ai.client._call_groq')
    def test_exhausted_budget_stops_the_chain_without_calling_any_provider(
        self, mock_groq, mock_gemini, mock_or,
    ):
        from ai.client import _call_with_fallback

        budget = RequestBudget(0)
        with self.assertRaises(BudgetExceeded):
            _call_with_fallback([{'role': 'user', 'content': 'hi'}], budget=budget)

        mock_groq.assert_not_called()
        mock_gemini.assert_not_called()
        mock_or.assert_not_called()
        self.assertEqual(budget.degraded_stages, [STAGE_AI_ENRICHMENT])

    @override_settings(AI_REQUEST_TIMEOUT_SECONDS=30)
    @patch('ai.client._call_openrouter')
    @patch('ai.client._call_gemini')
    @patch('ai.client._call_groq')
    def test_budget_running_out_mid_chain_is_not_reported_as_provider_failure(
        self, mock_groq, mock_gemini, mock_or,
    ):
        """Groq fails for real, then the budget runs out. The caller must see
        BudgetExceeded, not groq's RuntimeError - otherwise a deadline reads
        as 'every AI provider is down'."""
        from ai.client import _call_with_fallback

        budget = RequestBudget(600)

        def fail_and_drain(messages, timeout=None):
            budget._deadline = 0  # simulate the clock running out during the call
            raise RuntimeError('groq down')

        mock_groq.side_effect = fail_and_drain

        with self.assertRaises(BudgetExceeded):
            _call_with_fallback([{'role': 'user', 'content': 'hi'}], budget=budget)

        mock_gemini.assert_not_called()
        mock_or.assert_not_called()

    @override_settings(AI_REQUEST_TIMEOUT_SECONDS=30)
    @patch('ai.client._call_openrouter')
    @patch('ai.client._call_gemini')
    @patch('ai.client._call_groq')
    def test_all_providers_failing_with_budget_left_still_raises_the_provider_error(
        self, mock_groq, mock_gemini, mock_or,
    ):
        from ai.client import _call_with_fallback

        mock_groq.side_effect = RuntimeError('groq down')
        mock_gemini.side_effect = RuntimeError('gemini down')
        mock_or.side_effect = RuntimeError('openrouter down')
        budget = RequestBudget(600)

        with self.assertRaises(RuntimeError) as ctx:
            _call_with_fallback([{'role': 'user', 'content': 'hi'}], budget=budget)

        self.assertNotIsInstance(ctx.exception, BudgetExceeded)
        self.assertFalse(budget.exhausted)


class AISecurityEnrichmentBudgetTests(TestCase):
    """AISecurityService.enrich must keep the real findings and fall back to
    scanner text on budget exhaustion - and record it as a skipped stage
    rather than an AI failure."""

    @staticmethod
    def _finding():
        from analyses.services.types import SecurityFinding, Severity, VulnerabilityType

        return SecurityFinding(
            scanner='bandit', rule_id='B307', vulnerability_type=VulnerabilityType.COMMAND_INJECTION,
            severity=Severity.CRITICAL, description='eval used', line_number=1, code_snippet='eval(x)',
        )

    def test_exhausted_budget_keeps_findings_and_uses_scanner_text(self):
        from analyses.services.ai_security_service import AISecurityService

        budget = RequestBudget(0)
        findings = AISecurityService(budget=budget).enrich([self._finding()], 'eval(x)')

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].explanation, 'eval used')  # scanner-provided fallback
        self.assertTrue(findings[0].remediation)
        self.assertEqual(budget.degraded_stages, [STAGE_AI_ENRICHMENT])

    @patch('analyses.services.ai_security_service.generate_text')
    def test_healthy_budget_still_calls_the_ai(self, mock_generate):
        from analyses.services.ai_security_service import AISecurityService

        mock_generate.return_value = '[{"explanation": "ai text", "remediation": "ai fix"}]'
        budget = RequestBudget(600)

        findings = AISecurityService(budget=budget).enrich([self._finding()], 'eval(x)')

        self.assertEqual(findings[0].explanation, 'ai text')
        self.assertEqual(budget.degraded_stages, [])

    @patch('analyses.services.ai_security_service.generate_text')
    def test_a_real_ai_failure_is_not_recorded_as_budget_degradation(self, mock_generate):
        from analyses.services.ai_security_service import AISecurityService

        mock_generate.side_effect = RuntimeError('provider exploded')
        budget = RequestBudget(600)

        findings = AISecurityService(budget=budget).enrich([self._finding()], 'eval(x)')

        self.assertEqual(findings[0].explanation, 'eval used')  # same graceful fallback
        self.assertEqual(budget.degraded_stages, [])            # but NOT a budget problem
        self.assertFalse(budget.exhausted)


class BanditBudgetTests(TestCase):
    @patch('analyses.services.bandit_service.subprocess.run')
    def test_exhausted_budget_skips_bandit_without_running_it(self, mock_run):
        from analyses.services.bandit_service import REASON_BUDGET_EXHAUSTED, BanditScanner

        budget = RequestBudget(0)
        scanner = BanditScanner(budget=budget)

        self.assertEqual(scanner.scan('import os\n'), [])
        mock_run.assert_not_called()
        self.assertEqual(scanner.consume_unavailable().reason, REASON_BUDGET_EXHAUSTED)
        self.assertEqual(budget.degraded_stages, [STAGE_BANDIT])

    @patch('analyses.services.bandit_service.subprocess.run')
    def test_healthy_budget_clamps_but_still_runs_bandit(self, mock_run):
        from analyses.services.bandit_service import BanditScanner

        mock_run.return_value.stdout = '{"results": []}'
        BanditScanner(budget=RequestBudget(10)).scan('import os\n')

        mock_run.assert_called_once()
        self.assertLessEqual(mock_run.call_args.kwargs['timeout'], 10)

    @patch('analyses.services.bandit_service.subprocess.run')
    def test_unbudgeted_bandit_uses_its_full_timeout(self, mock_run):
        from analyses.services.bandit_service import BANDIT_TIMEOUT_SECONDS, BanditScanner

        mock_run.return_value.stdout = '{"results": []}'
        BanditScanner().scan('import os\n')

        self.assertEqual(mock_run.call_args.kwargs['timeout'], BANDIT_TIMEOUT_SECONDS)

    @patch('analyses.services.bandit_service.subprocess.run')
    def test_a_genuine_bandit_timeout_is_not_reported_as_budget_exhaustion(self, mock_run):
        import subprocess

        from analyses.services.bandit_service import BanditScanner

        mock_run.side_effect = subprocess.TimeoutExpired(cmd='bandit', timeout=20)
        budget = RequestBudget(600)  # plenty left - Bandit itself hung

        scanner = BanditScanner(budget=budget)
        scanner.scan('import os\n')

        self.assertEqual(scanner.consume_unavailable().reason, 'timeout')
        self.assertEqual(budget.degraded_stages, [])


class RuntimeCheckBudgetTests(TestCase):
    @patch('analyses.engine.sandbox.run_python')
    def test_exhausted_budget_skips_the_sandbox(self, mock_run):
        from analyses.engine import analyze_code

        budget = RequestBudget(0)
        result = analyze_code('x = 1\n', 'Python', budget=budget)

        mock_run.assert_not_called()
        types = {issue['type'] for issue in result['issues']}
        self.assertIn('runtime_check_skipped', types)
        self.assertEqual(budget.degraded_stages, [STAGE_RUNTIME_CHECK])

    @patch('analyses.engine.sandbox.run_python')
    def test_skipping_the_sandbox_does_not_move_the_quality_score(self, mock_run):
        from analyses.engine import analyze_code

        mock_run.return_value = {'status': 'ok'}
        full = analyze_code('x = 1\n', 'Python', budget=RequestBudget(600))
        skipped = analyze_code('x = 1\n', 'Python', budget=RequestBudget(0))

        self.assertEqual(full['quality_score'], skipped['quality_score'])

    @patch('analyses.engine.sandbox.run_python')
    def test_unbudgeted_analysis_still_runs_the_sandbox(self, mock_run):
        from analyses.engine import analyze_code

        mock_run.return_value = {'status': 'ok'}
        analyze_code('x = 1\n', 'Python')

        mock_run.assert_called_once()
