"""A security scanner that could not run must never render as a clean scan.

`report_generator.build_report` has always said so via
`scan_complete`/`scanners_unavailable`, and the Security Analysis Mode
endpoint serializes both. But `_analyze_file_content` - the shared entry point
for PR review, the single-file check and the context check - read only
`vulnerabilities` off that report and dropped the rest, so a missing/hung/
budget-skipped Bandit surfaced downstream as "no security issues found".
These cover that path.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from analyses.services.bandit_service import REASON_BUDGET_EXHAUSTED
from analyses.services.report_generator import SEVERITY_PENALTIES
from analyses.services.types import ScannerUnavailable
from core.execution_budget import RequestBudget

from ..models import FileAnalysis, RepositoryIndex
from ..services.comment_service import _format_comment_body, _format_issue_title
from ..services.pr_analysis_service import (
    SCANNER_UNAVAILABLE_ISSUE_TYPE,
    SCANNER_UNAVAILABLE_SEVERITY,
    PRAnalysisService,
    _analyze_file_content,
    _score_for_issues,
)
from .factories import make_integration, make_pr_analysis, make_repository, make_user


def _report(vulnerabilities=None, unavailable=None):
    """A SecurityAnalysisService.analyze() return value, in build_report's shape."""
    unavailable = unavailable or []
    return {
        'score': 100,
        'risk_level': 'minimal',
        'summary': {},
        'vulnerabilities': vulnerabilities or [],
        'scan_complete': not unavailable,
        'scanners_unavailable': [u.to_dict() for u in unavailable],
    }


class ScannerUnavailabilityReachesTheIssueListTests(TestCase):
    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_a_clean_scan_adds_nothing(self, mock_service):
        mock_service.return_value.analyze.return_value = _report()

        issues = _analyze_file_content('x = 1\n', 'Python')

        self.assertFalse(any(i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE for i in issues))

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_missing_bandit_is_not_a_clean_scan(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'not_installed', 'Bandit is not installed or not on PATH.'),
        ])

        issues = _analyze_file_content('x = 1\n', 'Python')

        notices = [i for i in issues if i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['source'], 'security')
        self.assertIn('bandit', notices[0]['message'])
        self.assertIn('not_installed', notices[0]['message'])
        self.assertIn('not installed', notices[0]['explanation'])

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_timed_out_bandit_is_not_a_clean_scan(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'timeout', 'Bandit did not finish within 20s.'),
        ])

        issues = _analyze_file_content('x = 1\n', 'Python')

        notices = [i for i in issues if i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE]
        self.assertIn('timeout', notices[0]['message'])

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_unparsable_output_is_not_a_clean_scan(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'unparsable_output', 'Bandit output was not valid JSON.'),
        ])

        issues = _analyze_file_content('x = 1\n', 'Python')

        self.assertIn('unparsable_output', issues[-1]['message'])

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_a_scanner_that_raised_is_not_a_clean_scan(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'error', 'Scanner raised an unexpected error.'),
        ])

        issues = _analyze_file_content('x = 1\n', 'Python')

        self.assertTrue(any(i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE for i in issues))

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_every_unavailable_scanner_gets_its_own_notice(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'timeout', ''),
            ScannerUnavailable('custom_rules', 'error', ''),
        ])

        issues = _analyze_file_content('x = 1\n', 'Python')

        notices = [i for i in issues if i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE]
        self.assertEqual(len(notices), 2)
        messages = ' '.join(n['message'] for n in notices)
        self.assertIn('bandit', messages)
        self.assertIn('custom_rules', messages)

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_real_vulnerabilities_are_preserved_alongside_the_notice(self, mock_service):
        """The notice must be additive - an incomplete scan that still found
        something must not lose what it found."""
        mock_service.return_value.analyze.return_value = _report(
            vulnerabilities=[{
                'vulnerability_type': 'hardcoded_secret', 'severity': 'critical', 'line_number': 1,
                'title': 'Hardcoded Secret', 'explanation': 'e', 'remediation': 'r',
            }],
            unavailable=[ScannerUnavailable('bandit', 'timeout', '')],
        )

        issues = _analyze_file_content('x = 1\n', 'Python')

        by_type = {i['type']: i for i in issues}
        self.assertIn('hardcoded_secret', by_type)
        self.assertEqual(by_type['hardcoded_secret']['severity'], 'critical')
        self.assertIn(SCANNER_UNAVAILABLE_ISSUE_TYPE, by_type)


class ScannerUnavailabilityDoesNotMoveTheScoreTests(TestCase):
    """build_report deliberately refuses to let an unavailable scanner change
    the score - a scanner failing is not evidence of vulnerabilities, and
    inventing a penalty would mislead in the other direction. The notice must
    keep that property once it becomes an issue."""

    def test_the_notice_severity_carries_no_penalty(self):
        self.assertNotIn(SCANNER_UNAVAILABLE_SEVERITY, [s.value for s in SEVERITY_PENALTIES])
        self.assertEqual(SEVERITY_PENALTIES.get(SCANNER_UNAVAILABLE_SEVERITY, 0), 0)

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_score_is_identical_with_and_without_the_notice(self, mock_service):
        mock_service.return_value.analyze.return_value = _report()
        clean_score = _score_for_issues(_analyze_file_content('x = 1\n', 'Python'))

        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'not_installed', ''),
        ])
        degraded_score = _score_for_issues(_analyze_file_content('x = 1\n', 'Python'))

        self.assertEqual(clean_score, degraded_score)


class ScannerUnavailabilityRendersInThePRCommentTests(TestCase):
    """The notice has line=None, so comment_service routes it to the summary
    body's 'Additional findings' list rather than an inline comment. It must
    render there rather than crash on an unknown severity."""

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_the_notice_formats_without_a_known_severity_label(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'timeout', 'Bandit did not finish within 20s.'),
        ])
        notice = [
            i for i in _analyze_file_content('x = 1\n', 'Python')
            if i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE
        ][0]

        title = _format_issue_title(notice)
        body = _format_comment_body(notice)

        self.assertIn('Info', title)
        self.assertIn('Scanner Unavailable', title)
        self.assertIn('did not finish', body)


class PRSummaryDisclosesAnIncompleteScanTests(TestCase):
    def setUp(self):
        self.pr_analysis = make_pr_analysis(make_repository(make_integration(make_user())))

    def _file_analysis(self, path, issues):
        return FileAnalysis.objects.create(
            pull_request_analysis=self.pr_analysis, file_path=path,
            language='Python', issues=issues, score=100.0,
        )

    def test_a_fully_clean_review_says_no_issues_found(self):
        results = [(self._file_analysis('a.py', []), '')]

        summary = PRAnalysisService._build_summary(results)

        self.assertIn('No issues found', summary)
        self.assertNotIn('incomplete', summary)

    def test_an_incomplete_scan_is_disclosed_in_the_summary(self):
        notice = {
            'source': 'security', 'type': SCANNER_UNAVAILABLE_ISSUE_TYPE,
            'severity': SCANNER_UNAVAILABLE_SEVERITY, 'line': None,
            'message': 'Security scan incomplete: the bandit scanner did not run (timeout).',
            'explanation': '', 'remediation': '',
        }
        results = [(self._file_analysis('a.py', [notice]), '')]

        summary = PRAnalysisService._build_summary(results)

        self.assertNotIn('No issues found', summary)
        self.assertIn('Security scanning was incomplete for 1 file(s)', summary)


@override_settings(GITHUB_MAX_FILE_SIZE_BYTES=500_000, GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS=0)
class BudgetSkippedScannerIsDisclosedTests(TestCase):
    """End-to-end tie-in with the request budget: a Bandit skipped because the
    budget ran out is `ScannerUnavailable(reason='budget_exhausted')`, and must
    reach the issue list like any other unavailability."""

    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))
        RepositoryIndex.objects.create(
            repository=self.repository, status=RepositoryIndex.Status.COMPLETED,
        )

    @patch('analyses.engine.sandbox.run_python')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_budget_skipped_bandit_appears_as_an_incomplete_scan(self, mock_client_cls, _mock_sandbox):
        mock_client_cls.return_value.get_file_content.return_value = 'import os\n'
        mock_client_cls.return_value.get_repository_tree.return_value = {'entries': [], 'truncated': False}

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.py', 'token')

        notices = [i for i in result['issues'] if i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE]
        self.assertEqual(len(notices), 1)
        self.assertIn(REASON_BUDGET_EXHAUSTED, notices[0]['message'])

    def test_the_budget_reason_is_distinct_from_a_real_scanner_timeout(self):
        self.assertNotEqual(REASON_BUDGET_EXHAUSTED, 'timeout')


class NoBudgetMeansNoBehaviourChangeTests(TestCase):
    """The notice comes from the report, not from the budget - an unbudgeted
    caller (PR review, single-file check) gets it too."""

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_unbudgeted_analysis_still_reports_unavailability(self, mock_service):
        mock_service.return_value.analyze.return_value = _report(unavailable=[
            ScannerUnavailable('bandit', 'not_installed', ''),
        ])

        issues = _analyze_file_content('x = 1\n', 'Python', budget=None)

        self.assertTrue(any(i['type'] == SCANNER_UNAVAILABLE_ISSUE_TYPE for i in issues))
        mock_service.assert_called_once_with(None)

    @patch('github_integration.services.pr_analysis_service._security_service')
    def test_a_budgeted_analysis_uses_the_same_path(self, mock_service):
        mock_service.return_value.analyze.return_value = _report()
        budget = RequestBudget(90)

        _analyze_file_content('x = 1\n', 'Python', budget=budget)

        mock_service.assert_called_once_with(budget)
