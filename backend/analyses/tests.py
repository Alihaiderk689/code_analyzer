from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from . import engine
from .models import Analysis

User = get_user_model()


class DetectLanguageTests(SimpleTestCase):
    def test_known_extensions_map_to_language(self):
        self.assertEqual(engine.detect_language('main.py'), 'Python')
        self.assertEqual(engine.detect_language('App.tsx'), 'TypeScript')
        self.assertEqual(engine.detect_language('Main.JAVA'), 'Java')

    def test_unknown_extension_returns_unknown(self):
        self.assertEqual(engine.detect_language('notes.txt'), 'Unknown')


class DetectLanguageFromCodeTests(SimpleTestCase):
    def test_empty_code_is_unknown(self):
        self.assertEqual(engine.detect_language_from_code(''), 'Unknown')
        self.assertEqual(engine.detect_language_from_code('   \n  '), 'Unknown')

    def test_python_snippet(self):
        code = 'import os\n\n\nclass Greeter:\n    def hello(self, name):\n        print(f"hi {name}")\n'
        self.assertEqual(engine.detect_language_from_code(code), 'Python')

    def test_python_main_guard(self):
        code = 'def main():\n    pass\n\n\nif __name__ == "__main__":\n    main()\n'
        self.assertEqual(engine.detect_language_from_code(code), 'Python')

    def test_javascript_snippet(self):
        code = 'function greet(name) {\n  console.log("hi " + name);\n}\n\nconst x = greet;\n'
        self.assertEqual(engine.detect_language_from_code(code), 'JavaScript')

    def test_typescript_snippet(self):
        code = 'interface User {\n  name: string;\n}\n\nfunction greet(user: User): void {\n  console.log(user.name);\n}\n'
        self.assertEqual(engine.detect_language_from_code(code), 'TypeScript')

    def test_java_snippet(self):
        code = (
            'public class Main {\n'
            '    public static void main(String[] args) {\n'
            '        System.out.println("hi");\n'
            '    }\n'
            '}\n'
        )
        self.assertEqual(engine.detect_language_from_code(code), 'Java')

    def test_cpp_snippet(self):
        code = '#include <iostream>\n\nint main() {\n    std::cout << "hi" << std::endl;\n}\n'
        self.assertEqual(engine.detect_language_from_code(code), 'C++')

    def test_go_snippet(self):
        code = 'package main\n\nimport "fmt"\n\nfunc main() {\n\tx := 1\n\tfmt.Println(x)\n}\n'
        self.assertEqual(engine.detect_language_from_code(code), 'Go')

    def test_php_snippet(self):
        code = '<?php\n$name = "world";\necho "hi " . $name;\n'
        self.assertEqual(engine.detect_language_from_code(code), 'PHP')

    def test_ambiguous_arithmetic_falls_back_to_python_via_valid_syntax(self):
        # No language-specific keywords at all - the only signal left is that it
        # happens to parse as valid Python, which is a legitimate (if weak) tiebreaker.
        self.assertEqual(engine.detect_language_from_code('x = 1 + 2\ny = x * 3\n'), 'Python')

    def test_genuinely_unrecognizable_code_is_unknown(self):
        self.assertEqual(engine.detect_language_from_code('!!! ??? @@@ ###\n'), 'Unknown')


class AnalyzeCodeGenericChecksTests(SimpleTestCase):
    def test_counts_only_non_blank_lines(self):
        result = engine.analyze_code('a = 1\n\n\nb = 2\n', language='Unknown')
        self.assertEqual(result['lines_of_code'], 2)

    def test_empty_code_scores_zero(self):
        result = engine.analyze_code('', language='Unknown')
        self.assertEqual(result['lines_of_code'], 0)
        self.assertEqual(result['quality_score'], 0.0)

    def test_detects_todo_marker(self):
        result = engine.analyze_code('x = 1  # TODO fix this\n', language='Unknown')
        types = [i['type'] for i in result['issues']]
        self.assertIn('todo', types)

    def test_detects_long_line(self):
        result = engine.analyze_code('x = ' + '1' * 200 + '\n', language='Unknown')
        types = [i['type'] for i in result['issues']]
        self.assertIn('long_line', types)

    def test_flags_missing_comments_on_long_file(self):
        code = '\n'.join(f'x{i} = {i}' for i in range(35))
        result = engine.analyze_code(code, language='Unknown')
        types = [i['type'] for i in result['issues']]
        self.assertIn('no_comments', types)

    def test_short_file_without_comments_not_flagged(self):
        result = engine.analyze_code('x = 1\ny = 2\n', language='Unknown')
        types = [i['type'] for i in result['issues']]
        self.assertNotIn('no_comments', types)

    def test_more_issues_lower_quality_score(self):
        clean = engine.analyze_code('x = 1\n', language='Unknown')
        with_todo = engine.analyze_code('x = 1  # TODO fix\n', language='Unknown')
        self.assertGreater(clean['quality_score'], with_todo['quality_score'])


class AnalyzeCodePythonStaticChecksTests(SimpleTestCase):
    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'ok'})
    def test_syntax_error_reported_and_skips_further_checks(self, _mock_sandbox):
        result = engine.analyze_code('def broken(:\n    pass\n', language='Python')
        types = [i['type'] for i in result['issues']]
        self.assertIn('syntax_error', types)
        # A syntax error short-circuits pyflakes/runtime checks entirely.
        self.assertEqual(len(result['issues']), 1)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'ok'})
    def test_unused_import_detected(self, _mock_sandbox):
        result = engine.analyze_code('import os\nx = 1\n', language='Python')
        types = [i['type'] for i in result['issues']]
        self.assertIn('unused_import', types)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'ok'})
    def test_undefined_name_detected(self, _mock_sandbox):
        result = engine.analyze_code('print(totally_undefined_name)\n', language='Python')
        types = [i['type'] for i in result['issues']]
        self.assertIn('undefined_name', types)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'ok'})
    def test_clean_code_has_no_static_issues(self, _mock_sandbox):
        result = engine.analyze_code('def add(a, b):\n    return a + b\n', language='Python')
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['quality_score'], 100.0)


class AnalyzeCodePythonSandboxTests(SimpleTestCase):
    """The real sandbox (sandbox-exec) is macOS-only, so these mock it directly to
    keep engine behavior testable/deterministic on any platform, including CI."""

    @patch('analyses.engine.sandbox.run_python')
    def test_runtime_error_from_sandbox_becomes_issue(self, mock_run):
        mock_run.return_value = {
            'status': 'error', 'exception_type': 'ZeroDivisionError', 'message': 'division by zero', 'line': 2,
        }
        result = engine.analyze_code('x = 1\ny = x / 0\n', language='Python')
        runtime_issues = [i for i in result['issues'] if i['type'] == 'runtime_error']
        self.assertEqual(len(runtime_issues), 1)
        self.assertIn('ZeroDivisionError', runtime_issues[0]['message'])
        self.assertEqual(runtime_issues[0]['line'], 2)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'timeout'})
    def test_timeout_from_sandbox_becomes_issue(self, _mock_run):
        result = engine.analyze_code('while True:\n    pass\n', language='Python')
        types = [i['type'] for i in result['issues']]
        self.assertIn('execution_timeout', types)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'unavailable'})
    def test_sandbox_unavailable_degrades_silently(self, _mock_run):
        result = engine.analyze_code('x = 1\n', language='Python')
        self.assertEqual(result['issues'], [])


def make_authenticated_client(email='owner@example.com'):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password='TestPass123!')
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


class AnalyzeUploadEndpointTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.post(reverse('analysis-analyze'), {'code': 'x = 1'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'unavailable'})
    def test_analyze_creates_completed_analysis_owned_by_caller(self, _mock_sandbox):
        response = self.client.post(reverse('analysis-analyze'), {
            'name': 'demo.py', 'code': 'import os\n',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'completed')
        analysis = Analysis.objects.get(pk=response.data['id'])
        self.assertEqual(analysis.owner, self.user)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'unavailable'})
    def test_analyze_detects_language_from_code_not_client_input(self, _mock_sandbox):
        # No 'language' field is even accepted anymore - the server decides, always,
        # from the code itself. This pins that a client can't just claim any language.
        response = self.client.post(reverse('analysis-analyze'), {
            'name': 'demo.py', 'language': 'PHP', 'code': 'import os\n',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['language'], 'Python')

    def test_analyze_rejects_empty_code(self):
        response = self.client.post(reverse('analysis-analyze'), {'code': '   '})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'unavailable'})
    def test_upload_detects_language_from_filename(self, _mock_sandbox):
        upload = SimpleUploadedFile('script.py', b'x = 1\n', content_type='text/plain')
        response = self.client.post(reverse('analysis-upload'), {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['language'], 'Python')

    def test_upload_rejects_oversized_file(self):
        upload = SimpleUploadedFile('big.py', b'x' * (2 * 1024 * 1024 + 1), content_type='text/plain')
        response = self.client.post(reverse('analysis-upload'), {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_upload_non_utf8_file_marked_failed_not_500(self):
        upload = SimpleUploadedFile('binary.py', b'\xff\xfe\x00\x01', content_type='application/octet-stream')
        response = self.client.post(reverse('analysis-upload'), {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'failed')


class AnalysisOwnershipTests(APITestCase):
    """Cross-user access must 404, not leak another user's analysis - the
    highest-value permission boundary in this app."""

    def setUp(self):
        self.client, self.owner = make_authenticated_client('owner2@example.com')
        self.other_client, self.other_user = make_authenticated_client('intruder@example.com')
        self.analysis = Analysis.objects.create(
            owner=self.owner, name='secret.py', language='Python', source_code='x = 1\n',
            status=Analysis.Status.COMPLETED, quality_score=100.0,
        )

    def test_owner_can_view_their_analysis(self):
        response = self.client.get(reverse('analysis-detail', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_user_cannot_view_analysis(self):
        response = self.other_client.get(reverse('analysis-detail', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_cannot_delete_analysis(self):
        response = self.other_client.delete(reverse('analysis-detail', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Analysis.objects.filter(pk=self.analysis.id).exists())

    def test_list_view_scoped_to_owner(self):
        response = self.other_client.get(reverse('analysis-list'))
        self.assertEqual(response.data['count'], 0)

        own = self.client.get(reverse('analysis-list'))
        self.assertEqual(own.data['count'], 1)


class AnalysisLifecycleTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client('lifecycle@example.com')

    @patch('analyses.engine.sandbox.run_python', return_value={'status': 'unavailable'})
    def test_reanalyze_recomputes_results(self, _mock_sandbox):
        analysis = Analysis.objects.create(
            owner=self.user, name='a.py', language='Python', source_code='import os\n',
            status=Analysis.Status.COMPLETED,
        )
        response = self.client.post(reverse('analysis-reanalyze', args=[analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'completed')
        self.assertGreater(response.data['issues_count'], 0)

    def test_reanalyze_without_source_code_rejected(self):
        analysis = Analysis.objects.create(
            owner=self.user, name='binary.dat', language='Unknown', source_code='',
            status=Analysis.Status.FAILED,
        )
        response = self.client.post(reverse('analysis-reanalyze', args=[analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_pending_analysis(self):
        analysis = Analysis.objects.create(
            owner=self.user, name='a.py', language='Python', status=Analysis.Status.PENDING,
        )
        response = self.client.post(reverse('analysis-cancel', args=[analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'cancelled')

    def test_cancel_completed_analysis_rejected(self):
        analysis = Analysis.objects.create(
            owner=self.user, name='a.py', language='Python', status=Analysis.Status.COMPLETED,
        )
        response = self.client.post(reverse('analysis-cancel', args=[analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_clear_history_only_deletes_own_analyses(self):
        other_client, other_user = make_authenticated_client('other-history@example.com')
        Analysis.objects.create(owner=self.user, name='mine.py', language='Python')
        Analysis.objects.create(owner=other_user, name='theirs.py', language='Python')

        response = self.client.delete(reverse('history-clear'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['deleted_count'], 1)
        self.assertTrue(Analysis.objects.filter(owner=other_user).exists())


class DashboardSummaryTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client('dash@example.com')

    def test_empty_dashboard_has_no_crashes_on_none_averages(self):
        response = self.client.get(reverse('dashboard-summary'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['stats']['total_analyses'], 0)
        self.assertIsNone(response.data['scores']['average_score'])

    def test_stats_aggregate_across_statuses(self):
        Analysis.objects.create(owner=self.user, name='a', language='Python',
                                 status=Analysis.Status.COMPLETED, quality_score=90.0, lines_of_code=10)
        Analysis.objects.create(owner=self.user, name='b', language='Python',
                                 status=Analysis.Status.FAILED)
        Analysis.objects.create(owner=self.user, name='c', language='JavaScript',
                                 status=Analysis.Status.COMPLETED, quality_score=50.0, lines_of_code=5)

        response = self.client.get(reverse('dashboard-stats'))
        self.assertEqual(response.data['total_analyses'], 3)
        self.assertEqual(response.data['completed'], 2)
        self.assertEqual(response.data['failed'], 1)
        self.assertEqual(response.data['total_lines_of_code'], 15)
        self.assertAlmostEqual(response.data['average_quality_score'], 70.0)

    def test_dashboard_scoped_to_owner(self):
        other_client, other_user = make_authenticated_client('dash-other@example.com')
        Analysis.objects.create(owner=other_user, name='not-mine', language='Python',
                                 status=Analysis.Status.COMPLETED, quality_score=10.0)

        response = self.client.get(reverse('dashboard-stats'))
        self.assertEqual(response.data['total_analyses'], 0)

    def test_quality_score_distribution_buckets(self):
        Analysis.objects.create(owner=self.user, name='excellent', language='Python',
                                 status=Analysis.Status.COMPLETED, quality_score=95.0)
        Analysis.objects.create(owner=self.user, name='poor', language='Python',
                                 status=Analysis.Status.COMPLETED, quality_score=20.0)

        response = self.client.get(reverse('dashboard-scores'))
        self.assertEqual(response.data['distribution']['excellent'], 1)
        self.assertEqual(response.data['distribution']['poor'], 1)

    def test_recent_analyses_respects_limit(self):
        for i in range(5):
            Analysis.objects.create(owner=self.user, name=f'a{i}', language='Python',
                                     status=Analysis.Status.COMPLETED, quality_score=100.0)

        response = self.client.get(reverse('dashboard-recent'), {'limit': 2})
        self.assertEqual(len(response.data['results']), 2)
        self.assertEqual(response.data['count'], 5)


class SearchTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client('search@example.com')
        Analysis.objects.create(owner=self.user, name='payment_processor.py', language='Python',
                                 status=Analysis.Status.COMPLETED)
        Analysis.objects.create(owner=self.user, name='utils.js', language='JavaScript',
                                 status=Analysis.Status.COMPLETED)

    def test_requires_query_param(self):
        response = self.client.get(reverse('search'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filters_by_name(self):
        response = self.client.get(reverse('search'), {'q': 'payment'})
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'payment_processor.py')

    def test_scoped_to_owner(self):
        other_client, other_user = make_authenticated_client('search-other@example.com')
        Analysis.objects.create(owner=other_user, name='payment_secret.py', language='Python')

        response = self.client.get(reverse('search'), {'q': 'payment'})
        self.assertEqual(response.data['count'], 1)


class SecurityAnalysisViewTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client('security@example.com')
        self.analysis = Analysis.objects.create(
            owner=self.user, name='vuln.py', language='Python',
            source_code='password = "hardcoded123"\n',
            status=Analysis.Status.COMPLETED, quality_score=90.0,
        )

    def test_post_requires_authentication(self):
        anon = APIClient()
        response = anon.post(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_post_404_for_another_users_analysis(self):
        other_client, _other_user = make_authenticated_client('intruder-sec@example.com')
        response = other_client.post(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_post_rejects_analysis_not_yet_completed(self):
        pending = Analysis.objects.create(owner=self.user, name='pending.py', language='Python', source_code='x = 1\n')
        response = self.client.post(reverse('analysis-security', args=[pending.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('analyses.security_views.SecurityAnalysisService')
    def test_post_runs_analysis_and_caches_on_the_model(self, mock_service_cls):
        mock_service_cls.return_value.analyze.return_value = {
            'score': 70, 'risk_level': 'medium',
            'summary': {'critical': 0, 'high': 0, 'medium': 1, 'low': 0, 'total': 1},
            'vulnerabilities': [{
                'id': 'bandit:B105:1', 'scanner': 'bandit', 'rule_id': 'B105',
                'vulnerability_type': 'hardcoded_password', 'severity': 'medium',
                'title': 'Hardcoded Password', 'description': 'Possible hardcoded password',
                'line_number': 1, 'code_snippet': 'password = "hardcoded123"',
                'confidence': 'MEDIUM', 'explanation': 'Anyone reading the source gets the password.',
                'remediation': 'Load it from an environment variable instead.',
            }],
        }

        response = self.client.post(reverse('analysis-security', args=[self.analysis.id]))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['score'], 70)
        self.assertEqual(response.data['risk_level'], 'medium')
        self.assertEqual(len(response.data['vulnerabilities']), 1)
        self.assertFalse(response.data['cached'])

        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.security_report['score'], 70)

    @patch('analyses.security_views.SecurityAnalysisService')
    def test_post_returns_cached_report_without_rerunning(self, mock_service_cls):
        mock_service_cls.return_value.analyze.return_value = {
            'score': 90, 'risk_level': 'low',
            'summary': {'critical': 0, 'high': 0, 'medium': 0, 'low': 1, 'total': 1},
            'vulnerabilities': [],
        }
        first = self.client.post(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(mock_service_cls.return_value.analyze.call_count, 1)

        second = self.client.post(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(mock_service_cls.return_value.analyze.call_count, 1)  # not called again
        self.assertTrue(second.data['cached'])
        self.assertEqual(second.data['score'], first.data['score'])

    @patch('analyses.security_views.SecurityAnalysisService')
    def test_post_regenerate_true_forces_a_rerun(self, mock_service_cls):
        mock_service_cls.return_value.analyze.return_value = {
            'score': 100, 'risk_level': 'minimal',
            'summary': {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'total': 0},
            'vulnerabilities': [],
        }
        self.client.post(reverse('analysis-security', args=[self.analysis.id]))
        self.client.post(f"{reverse('analysis-security', args=[self.analysis.id])}?regenerate=true")
        self.assertEqual(mock_service_cls.return_value.analyze.call_count, 2)

    @patch('analyses.security_views.SecurityAnalysisService')
    def test_post_service_exception_returns_503(self, mock_service_cls):
        mock_service_cls.return_value.analyze.side_effect = RuntimeError('boom')
        response = self.client.post(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.security_report, {})

    def test_get_requires_authentication(self):
        anon = APIClient()
        response = anon.get(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_get_404_when_never_run(self):
        response = self.client.get(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('analyses.security_views.SecurityAnalysisService')
    def test_get_returns_cached_report_after_post(self, mock_service_cls):
        mock_service_cls.return_value.analyze.return_value = {
            'score': 85, 'risk_level': 'low',
            'summary': {'critical': 0, 'high': 0, 'medium': 0, 'low': 1, 'total': 1},
            'vulnerabilities': [],
        }
        self.client.post(reverse('analysis-security', args=[self.analysis.id]))

        response = self.client.get(reverse('analysis-security', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['score'], 85)
        self.assertTrue(response.data['cached'])

    @patch('analyses.services.ai_security_service.generate_text')
    def test_end_to_end_with_real_scanners_through_the_real_view(self, mock_generate):
        """Only the network-calling AI step is mocked - Bandit and the custom
        rules scanner run for real, through the real HTTP view, proving the
        whole pipeline (subprocess -> parse -> dedupe -> enrich -> score ->
        serialize) actually works end to end, not just each piece in isolation."""
        mock_generate.return_value = '[{"explanation": "Anyone reading the source gets the password.", "remediation": "Load it from an environment variable."}]'

        response = self.client.post(reverse('analysis-security', args=[self.analysis.id]))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertLess(response.data['score'], 100)
        self.assertTrue(response.data['vulnerabilities'])
        finding = response.data['vulnerabilities'][0]
        self.assertEqual(finding['explanation'], 'Anyone reading the source gets the password.')
        self.assertEqual(finding['remediation'], 'Load it from an environment variable.')


class SuggestionsViewTests(APITestCase):
    """SuggestionsView is otherwise untested (pre-existing gap) - covering it
    now since this change touches its response shape directly."""

    def setUp(self):
        self.client, self.user = make_authenticated_client('suggestions@example.com')
        self.analysis = Analysis.objects.create(
            owner=self.user, name='snippet.py', language='Python', source_code='x = 1\n',
            status=Analysis.Status.COMPLETED, quality_score=90.0,
        )

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.get(reverse('analysis-suggestions', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_404_for_another_users_analysis(self):
        other_client, _other_user = make_authenticated_client('intruder-sugg@example.com')
        response = other_client.get(reverse('analysis-suggestions', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rejects_analysis_not_yet_completed(self):
        pending = Analysis.objects.create(owner=self.user, name='pending.py', language='Python', source_code='x = 1\n')
        response = self.client.get(reverse('analysis-suggestions', args=[pending.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('analyses.ai_views.generate_text')
    def test_parses_categorized_suggestions_from_ai(self, mock_generate):
        mock_generate.return_value = (
            '[{"category": "security", "text": "Use parameterized queries."}, '
            '{"category": "general", "text": "Add a docstring."}]'
        )
        response = self.client.get(reverse('analysis-suggestions', args=[self.analysis.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['suggestions'], [
            {'category': 'security', 'text': 'Use parameterized queries.'},
            {'category': 'general', 'text': 'Add a docstring.'},
        ])
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.ai_suggestions[0]['category'], 'security')

    @patch('analyses.ai_views.generate_text')
    def test_invalid_category_from_ai_coerced_to_general(self, mock_generate):
        mock_generate.return_value = '[{"category": "made_up_category", "text": "Something."}]'
        response = self.client.get(reverse('analysis-suggestions', args=[self.analysis.id]))
        self.assertEqual(response.data['suggestions'], [{'category': 'general', 'text': 'Something.'}])

    @patch('analyses.ai_views.generate_text')
    def test_non_json_response_falls_back_to_uncategorized_lines(self, mock_generate):
        mock_generate.return_value = '- Add type hints\n- Handle the empty-list case\n'
        response = self.client.get(reverse('analysis-suggestions', args=[self.analysis.id]))
        self.assertEqual(response.data['suggestions'], [
            {'category': 'general', 'text': 'Add type hints'},
            {'category': 'general', 'text': 'Handle the empty-list case'},
        ])

    def test_old_flat_string_cache_is_upgraded_on_read(self):
        # Simulates an analysis that got its suggestions cached before
        # categories existed - a plain list of strings.
        self.analysis.ai_suggestions = ['Add a docstring.', 'Rename this variable.']
        self.analysis.save(update_fields=['ai_suggestions'])

        response = self.client.get(reverse('analysis-suggestions', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['suggestions'], [
            {'category': 'general', 'text': 'Add a docstring.'},
            {'category': 'general', 'text': 'Rename this variable.'},
        ])
        self.assertTrue(response.data['cached'])

    @patch('analyses.ai_views.generate_text')
    def test_regenerate_bypasses_cache(self, mock_generate):
        self.analysis.ai_suggestions = [{'category': 'general', 'text': 'Old suggestion.'}]
        self.analysis.save(update_fields=['ai_suggestions'])
        mock_generate.return_value = '[{"category": "security", "text": "New suggestion."}]'

        response = self.client.get(f"{reverse('analysis-suggestions', args=[self.analysis.id])}?regenerate=true")

        self.assertFalse(response.data['cached'])
        self.assertEqual(response.data['suggestions'], [{'category': 'security', 'text': 'New suggestion.'}])

    @patch('analyses.ai_views.generate_text', side_effect=RuntimeError('groq down'))
    def test_ai_failure_returns_503(self, _mock_generate):
        response = self.client.get(reverse('analysis-suggestions', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
