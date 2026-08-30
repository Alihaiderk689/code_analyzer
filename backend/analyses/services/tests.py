import subprocess
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from .ai_security_service import AISecurityService
from .bandit_service import BanditScanner
from .custom_rules_service import CustomRulesScanner
from .report_generator import SecurityReportGenerator
from .security_service import SecurityAnalysisService
from .types import BaseSecurityScanner, SecurityFinding, Severity, VulnerabilityType


def make_finding(vuln_type=VulnerabilityType.OTHER, severity=Severity.LOW, line=1, scanner='test') -> SecurityFinding:
    return SecurityFinding(
        scanner=scanner, rule_id='T1', vulnerability_type=vuln_type, severity=severity,
        description='test finding', line_number=line, code_snippet='x = 1',
    )


class BanditScannerTests(SimpleTestCase):
    """Runs the real bandit binary - these are integration tests, not mocked,
    to prove the actual subprocess/JSON-parsing pipeline works end to end."""

    def setUp(self):
        self.scanner = BanditScanner()

    def test_detects_sql_injection(self):
        code = "def get_user(uid):\n    query = \"SELECT * FROM users WHERE id = \" + uid\n    return query\n"
        findings = self.scanner.scan(code)
        self.assertTrue(any(f.vulnerability_type == VulnerabilityType.SQL_INJECTION for f in findings))

    def test_detects_hardcoded_password(self):
        code = 'password = "SuperSecret123"\n'
        findings = self.scanner.scan(code)
        self.assertTrue(any(f.vulnerability_type == VulnerabilityType.HARDCODED_PASSWORD for f in findings))

    def test_detects_unsafe_deserialization(self):
        code = 'import pickle\n\ndef load(data):\n    return pickle.loads(data)\n'
        findings = self.scanner.scan(code)
        self.assertTrue(any(f.vulnerability_type == VulnerabilityType.UNSAFE_DESERIALIZATION for f in findings))

    def test_detects_weak_randomness(self):
        code = 'import random\n\ndef token():\n    return random.random()\n'
        findings = self.scanner.scan(code)
        self.assertTrue(any(f.vulnerability_type == VulnerabilityType.WEAK_RANDOMNESS for f in findings))

    def test_detects_command_injection(self):
        code = 'import subprocess\n\ndef run(cmd):\n    subprocess.call(cmd, shell=True)\n'
        findings = self.scanner.scan(code)
        self.assertTrue(any(f.vulnerability_type == VulnerabilityType.COMMAND_INJECTION for f in findings))

    def test_escalates_eval_to_critical(self):
        code = 'def run(expr):\n    return eval(expr)\n'
        findings = self.scanner.scan(code)
        eval_findings = [f for f in findings if f.rule_id == 'B307']
        self.assertTrue(eval_findings)
        self.assertEqual(eval_findings[0].severity, Severity.CRITICAL)

    def test_clean_code_has_no_findings(self):
        code = 'def add(a, b):\n    return a + b\n'
        self.assertEqual(self.scanner.scan(code), [])

    def test_syntax_error_returns_empty_list_not_a_crash(self):
        code = 'def broken(:\n    pass\n'
        self.assertEqual(self.scanner.scan(code), [])

    def test_finding_includes_line_number_and_snippet(self):
        code = 'x = 1\npassword = "abcdefgh"\n'
        findings = self.scanner.scan(code)
        password_finding = next(f for f in findings if f.vulnerability_type == VulnerabilityType.HARDCODED_PASSWORD)
        self.assertEqual(password_finding.line_number, 2)
        self.assertIn('password', password_finding.code_snippet)

    @patch('analyses.services.bandit_service.subprocess.run', side_effect=FileNotFoundError())
    def test_missing_binary_degrades_to_empty_list(self, _mock_run):
        self.assertEqual(self.scanner.scan('x = 1\n'), [])


class CustomRulesScannerTests(SimpleTestCase):
    def setUp(self):
        self.scanner = CustomRulesScanner()

    def _types(self, code):
        return {f.vulnerability_type for f in self.scanner.scan(code)}

    def test_detects_debug_true(self):
        self.assertIn(VulnerabilityType.DEBUG_ENABLED, self._types('DEBUG = True\n'))

    def test_does_not_flag_debug_false(self):
        self.assertNotIn(VulnerabilityType.DEBUG_ENABLED, self._types('DEBUG = False\n'))

    def test_detects_csrf_exempt(self):
        code = '@csrf_exempt\ndef my_view(request):\n    pass\n'
        self.assertIn(VulnerabilityType.CSRF_DISABLED, self._types(code))

    def test_detects_hardcoded_secret_key(self):
        code = 'SECRET_KEY = "django-insecure-abcdefghijklmnop"\n'
        self.assertIn(VulnerabilityType.HARDCODED_SECRET, self._types(code))

    def test_does_not_flag_secret_loaded_from_env(self):
        code = "SECRET_KEY = os.environ['SECRET_KEY']\n"
        self.assertNotIn(VulnerabilityType.HARDCODED_SECRET, self._types(code))

    def test_detects_path_traversal_inline(self):
        code = 'def read(request):\n    return open(request.GET["path"]).read()\n'
        self.assertIn(VulnerabilityType.PATH_TRAVERSAL, self._types(code))

    def test_detects_path_traversal_via_assigned_variable(self):
        # The realistic shape: tainted input assigned to a variable, used a
        # couple of lines later - not all on one line with open().
        code = (
            'def read_file(request):\n'
            '    path = request.GET["path"]\n'
            '    return open(path).read()\n'
        )
        self.assertIn(VulnerabilityType.PATH_TRAVERSAL, self._types(code))

    def test_does_not_flag_open_on_untainted_variable(self):
        code = (
            'def read_config():\n'
            '    path = "config.json"\n'
            '    return open(path).read()\n'
        )
        self.assertNotIn(VulnerabilityType.PATH_TRAVERSAL, self._types(code))

    def test_detects_unsafe_file_upload(self):
        code = 'def upload(request):\n    f = request.FILES["file"]\n'
        self.assertIn(VulnerabilityType.UNSAFE_FILE_UPLOAD, self._types(code))

    def test_detects_missing_authentication_on_view_function(self):
        code = 'def dashboard(request):\n    return render(request, "dashboard.html")\n'
        self.assertIn(VulnerabilityType.MISSING_AUTHENTICATION, self._types(code))

    def test_login_required_decorator_suppresses_missing_auth(self):
        code = '@login_required\ndef dashboard(request):\n    return render(request, "dashboard.html")\n'
        self.assertNotIn(VulnerabilityType.MISSING_AUTHENTICATION, self._types(code))

    def test_detects_missing_authorization_on_view_class(self):
        code = 'class SecretView(APIView):\n    def get(self, request):\n        return Response({})\n'
        self.assertIn(VulnerabilityType.MISSING_AUTHORIZATION, self._types(code))

    def test_permission_classes_suppresses_missing_authorization(self):
        code = (
            'class SecretView(APIView):\n'
            '    permission_classes = [IsAuthenticated]\n'
            '    def get(self, request):\n'
            '        return Response({})\n'
        )
        self.assertNotIn(VulnerabilityType.MISSING_AUTHORIZATION, self._types(code))

    def test_clean_django_code_has_no_findings(self):
        code = (
            '@login_required\n'
            'def dashboard(request):\n'
            '    return render(request, "dashboard.html")\n'
        )
        self.assertEqual(self.scanner.scan(code), [])

    def test_every_finding_has_a_line_number_and_snippet(self):
        code = 'DEBUG = True\n@csrf_exempt\ndef v(request):\n    pass\n'
        for finding in self.scanner.scan(code):
            self.assertIsNotNone(finding.line_number)
            self.assertTrue(finding.code_snippet)


class SecurityReportGeneratorTests(SimpleTestCase):
    def setUp(self):
        self.generator = SecurityReportGenerator()

    def test_no_findings_scores_100_and_minimal_risk(self):
        report = self.generator.build_report([])
        self.assertEqual(report['score'], 100)
        self.assertEqual(report['risk_level'], 'minimal')
        self.assertEqual(report['summary']['total'], 0)

    def test_deducts_correct_points_per_severity(self):
        findings = [
            make_finding(severity=Severity.CRITICAL, line=1),
            make_finding(severity=Severity.HIGH, line=2),
            make_finding(severity=Severity.MEDIUM, line=3),
            make_finding(severity=Severity.LOW, line=4),
        ]
        report = self.generator.build_report(findings)
        # 100 - 30 - 20 - 10 - 5 = 35
        self.assertEqual(report['score'], 35)

    def test_score_never_goes_below_zero(self):
        findings = [make_finding(severity=Severity.CRITICAL, line=i) for i in range(10)]
        report = self.generator.build_report(findings)
        self.assertEqual(report['score'], 0)

    def test_risk_level_thresholds(self):
        cases = [
            (100, 'minimal'), (95, 'minimal'), (94, 'low'),
            (80, 'low'), (79, 'medium'), (60, 'medium'),
            (59, 'high'), (40, 'high'), (39, 'critical'), (0, 'critical'),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(self.generator._risk_level(score).value, expected)

    def test_summary_counts_by_severity(self):
        findings = [
            make_finding(severity=Severity.CRITICAL, line=1),
            make_finding(severity=Severity.CRITICAL, line=2),
            make_finding(severity=Severity.LOW, line=3),
        ]
        summary = self.generator.build_report(findings)['summary']
        self.assertEqual(summary['critical'], 2)
        self.assertEqual(summary['low'], 1)
        self.assertEqual(summary['high'], 0)
        self.assertEqual(summary['total'], 3)

    def test_vulnerabilities_sorted_by_severity_then_line(self):
        findings = [
            make_finding(severity=Severity.LOW, line=1),
            make_finding(severity=Severity.CRITICAL, line=5),
            make_finding(severity=Severity.HIGH, line=2),
        ]
        report = self.generator.build_report(findings)
        severities = [v['severity'] for v in report['vulnerabilities']]
        self.assertEqual(severities, ['critical', 'high', 'low'])


class AISecurityServiceTests(SimpleTestCase):
    def setUp(self):
        self.service = AISecurityService()

    def test_empty_findings_short_circuits_without_calling_ai(self):
        with patch('analyses.services.ai_security_service.generate_text') as mock_generate:
            result = self.service.enrich([], 'x = 1')
        mock_generate.assert_not_called()
        self.assertEqual(result, [])

    @patch('analyses.services.ai_security_service.generate_text')
    def test_attaches_explanation_and_remediation_from_ai_response(self, mock_generate):
        mock_generate.return_value = (
            '[{"explanation": "It lets attackers run arbitrary SQL.", "remediation": "Use parameterized queries."}]'
        )
        findings = [make_finding()]
        result = self.service.enrich(findings, 'x = 1')
        self.assertEqual(result[0].explanation, 'It lets attackers run arbitrary SQL.')
        self.assertEqual(result[0].remediation, 'Use parameterized queries.')

    @patch('analyses.services.ai_security_service.generate_text')
    def test_strips_markdown_code_fences(self, mock_generate):
        mock_generate.return_value = '```json\n[{"explanation": "e", "remediation": "r"}]\n```'
        result = self.service.enrich([make_finding()], 'x = 1')
        self.assertEqual(result[0].explanation, 'e')

    @patch('analyses.services.ai_security_service.generate_text', side_effect=RuntimeError('groq down'))
    def test_ai_exception_falls_back_gracefully(self, _mock_generate):
        result = self.service.enrich([make_finding()], 'x = 1')
        self.assertTrue(result[0].explanation)
        self.assertTrue(result[0].remediation)

    @patch('analyses.services.ai_security_service.generate_text', return_value='not json at all')
    def test_unparseable_response_falls_back_gracefully(self, _mock_generate):
        result = self.service.enrich([make_finding()], 'x = 1')
        self.assertTrue(result[0].explanation)

    @patch('analyses.services.ai_security_service.generate_text')
    def test_mismatched_response_length_is_padded_not_crashed(self, mock_generate):
        # Model only returns 1 entry for 2 findings.
        mock_generate.return_value = '[{"explanation": "e1", "remediation": "r1"}]'
        result = self.service.enrich([make_finding(line=1), make_finding(line=2)], 'x = 1')
        self.assertEqual(result[0].explanation, 'e1')
        self.assertTrue(result[1].explanation)  # fallback text, not a crash

    @patch('analyses.services.ai_security_service.generate_text', return_value='[]')
    def test_code_snippet_is_delimited_and_flagged_as_untrusted(self, mock_generate):
        self.service.enrich([make_finding()], 'x = 1')
        prompt, system_instruction = mock_generate.call_args.args[0], mock_generate.call_args.args[1]
        self.assertIn('BEGIN CODE SNIPPET', prompt)
        self.assertIn('END CODE SNIPPET', prompt)
        self.assertIn('untrusted data', system_instruction.lower())


class RecordingScanner(BaseSecurityScanner):
    """Test double - records calls and returns a fixed list of findings."""
    name = 'recording'

    def __init__(self, findings=None, raises=False):
        self._findings = findings or []
        self._raises = raises
        self.calls = []

    def scan(self, source_code, filename='submission.py', settings_source=''):
        self.calls.append((source_code, filename))
        if self._raises:
            raise RuntimeError('scanner exploded')
        return self._findings


class SecurityAnalysisServiceTests(SimpleTestCase):
    def _service_with(self, scanners, ai_enrich_passthrough=True):
        service = SecurityAnalysisService(scanners=scanners)
        if ai_enrich_passthrough:
            service.ai_service = _PassthroughAI()
        return service

    def test_aggregates_findings_from_all_scanners(self):
        scanner_a = RecordingScanner([make_finding(line=1)])
        scanner_a.name = 'a'
        scanner_b = RecordingScanner([make_finding(line=2)])
        scanner_b.name = 'b'
        report = self._service_with([scanner_a, scanner_b]).analyze('x = 1', 'Python')
        self.assertEqual(report['summary']['total'], 2)

    def test_one_failing_scanner_does_not_take_down_the_others(self):
        broken = RecordingScanner(raises=True)
        broken.name = 'broken'
        working = RecordingScanner([make_finding(line=1)])
        working.name = 'working'
        report = self._service_with([broken, working]).analyze('x = 1', 'Python')
        self.assertEqual(report['summary']['total'], 1)

    def test_deduplicates_same_type_and_line_across_scanners(self):
        scanner_a = RecordingScanner([make_finding(vuln_type=VulnerabilityType.SQL_INJECTION, line=5)])
        scanner_a.name = 'a'
        scanner_b = RecordingScanner([make_finding(vuln_type=VulnerabilityType.SQL_INJECTION, line=5)])
        scanner_b.name = 'b'
        report = self._service_with([scanner_a, scanner_b]).analyze('x = 1', 'Python')
        self.assertEqual(report['summary']['total'], 1)

    def test_bandit_is_skipped_for_non_python_language(self):
        bandit_like = RecordingScanner([make_finding()])
        bandit_like.name = 'bandit'
        report = self._service_with([bandit_like]).analyze('const x = 1;', 'JavaScript')
        self.assertEqual(len(bandit_like.calls), 0)
        self.assertEqual(report['summary']['total'], 0)

    def test_non_restricted_scanner_runs_for_any_language(self):
        custom_like = RecordingScanner([make_finding()])
        custom_like.name = 'custom_rules'
        report = self._service_with([custom_like]).analyze('const x = 1;', 'JavaScript')
        self.assertEqual(len(custom_like.calls), 1)
        self.assertEqual(report['summary']['total'], 1)


class _PassthroughAI:
    """Stand-in for AISecurityService in orchestration tests - skips the real
    AI call entirely so these tests aren't coupled to prompt/response format."""

    def enrich(self, findings, source_code):
        return findings


class ScannerUnavailabilityTests(TestCase):
    """A scanner that cannot run must never be indistinguishable from a clean
    result. Bandit returning [] on a missing binary previously rendered as a
    security report with zero vulnerabilities - false assurance on a security
    feature, which is worse than an error."""

    def _service(self):
        # AI enrichment is irrelevant here and would make a network call.
        return SecurityAnalysisService(ai_service=Mock(enrich=lambda findings, src: findings))

    def test_missing_bandit_binary_marks_the_scan_incomplete(self):
        with patch('analyses.services.bandit_service.subprocess.run', side_effect=FileNotFoundError):
            report = self._service().analyze(source_code='x = 1\n', language='Python')

        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['scanners_unavailable'][0]['scanner'], 'bandit')
        self.assertEqual(report['scanners_unavailable'][0]['reason'], 'not_installed')

    def test_bandit_timeout_marks_the_scan_incomplete(self):
        with patch(
            'analyses.services.bandit_service.subprocess.run',
            side_effect=subprocess.TimeoutExpired(cmd='bandit', timeout=1),
        ):
            report = self._service().analyze(source_code='x = 1\n', language='Python')

        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['scanners_unavailable'][0]['reason'], 'timeout')

    def test_unparsable_bandit_output_marks_the_scan_incomplete(self):
        result = Mock(stdout='not json at all', stderr='')
        with patch('analyses.services.bandit_service.subprocess.run', return_value=result):
            report = self._service().analyze(source_code='x = 1\n', language='Python')

        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['scanners_unavailable'][0]['reason'], 'unparsable_output')

    def test_partial_results_are_preserved_when_one_scanner_fails(self):
        """The other scanners still run - a partial report beats no report,
        as long as the report says it is partial."""
        with patch('analyses.services.bandit_service.subprocess.run', side_effect=FileNotFoundError):
            report = self._service().analyze(
                # Flagged by CustomRulesScanner, which is unaffected by Bandit
                # being unavailable.
                source_code='API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n',
                language='Python',
            )

        self.assertFalse(report['scan_complete'])
        self.assertTrue(report['vulnerabilities'], 'custom rules scanner should still contribute findings')

    def test_a_scanner_raising_is_reported_not_swallowed(self):
        broken = Mock(name='broken')
        broken.name = 'broken'
        broken.scan.side_effect = RuntimeError('tool exploded')
        service = SecurityAnalysisService(
            scanners=[broken], ai_service=Mock(enrich=lambda findings, src: findings),
        )

        report = service.analyze(source_code='x = 1\n', language='Python')

        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['scanners_unavailable'][0]['reason'], 'error')

    def test_healthy_scan_reports_complete(self):
        report = self._service().analyze(source_code='x = 1\n', language='Python')

        self.assertTrue(report['scan_complete'])
        self.assertEqual(report['scanners_unavailable'], [])

    def test_language_mismatch_is_not_reported_as_unavailable(self):
        """Skipping a Python-only scanner for JavaScript is correct behavior,
        not a failure - it must not flag the scan incomplete."""
        report = self._service().analyze(source_code='const a = 1;\n', language='JavaScript')

        self.assertTrue(report['scan_complete'])
