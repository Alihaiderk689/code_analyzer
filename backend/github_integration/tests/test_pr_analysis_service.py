from unittest.mock import patch

from django.test import TestCase, override_settings

from ..models import PullRequestAnalysis, RepositoryFileNode, RepositoryIndex
from ..services.github_client import GitHubAPIError
from ..services.pr_analysis_service import MAX_CONTEXT_RELATED_FILES, FileSkipReason, PRAnalysisService, _classify_file
from .factories import make_integration, make_pr_analysis, make_repository, make_user


class ClassifyFileTests(TestCase):
    def test_removed_file_is_skipped(self):
        language, reason = _classify_file('app.py', 'removed', has_patch=False)
        self.assertIsNone(language)
        self.assertEqual(reason, FileSkipReason.REMOVED)

    def test_binary_file_without_patch_is_skipped(self):
        language, reason = _classify_file('logo.png', 'added', has_patch=False)
        self.assertEqual(reason, FileSkipReason.BINARY)

    def test_lock_file_is_skipped(self):
        language, reason = _classify_file('package-lock.json', 'modified', has_patch=True)
        self.assertEqual(reason, FileSkipReason.LOCK_FILE)

    def test_go_sum_is_skipped(self):
        _language, reason = _classify_file('go.sum', 'modified', has_patch=True)
        self.assertEqual(reason, FileSkipReason.LOCK_FILE)

    def test_minified_js_is_treated_as_generated(self):
        _language, reason = _classify_file('static/app.min.js', 'modified', has_patch=True)
        self.assertEqual(reason, FileSkipReason.GENERATED)

    def test_vendored_path_is_treated_as_generated(self):
        _language, reason = _classify_file('vendor/lib/thing.go', 'modified', has_patch=True)
        self.assertEqual(reason, FileSkipReason.GENERATED)

    def test_unsupported_extension_is_skipped(self):
        _language, reason = _classify_file('README.md', 'modified', has_patch=True)
        self.assertEqual(reason, FileSkipReason.UNSUPPORTED_LANGUAGE)

    def test_supported_python_file_returns_language_and_no_skip_reason(self):
        language, reason = _classify_file('app.py', 'modified', has_patch=True)
        self.assertEqual(language, 'Python')
        self.assertIsNone(reason)

    def test_all_required_languages_are_supported(self):
        for filename, expected_language in [
            ('a.py', 'Python'), ('a.js', 'JavaScript'), ('a.jsx', 'JavaScript'),
            ('a.ts', 'TypeScript'), ('a.tsx', 'TypeScript'), ('a.java', 'Java'),
            ('a.cs', 'C#'), ('a.cpp', 'C++'), ('a.go', 'Go'), ('a.rs', 'Rust'), ('a.php', 'PHP'),
        ]:
            language, reason = _classify_file(filename, 'modified', has_patch=True)
            self.assertEqual(language, expected_language, filename)
            self.assertIsNone(reason, filename)


_CLEAN_PYTHON_FILE = 'def add(a, b):\n    return a + b\n'

_PATCH_FOR_CLEAN_FILE = '@@ -0,0 +1,2 @@\n+def add(a, b):\n+    return a + b'

# Triggers CustomRulesScanner's CUSTOM_HARDCODED_SECRET rule (language-agnostic,
# no Bandit/AI dependency) so a real, deterministic security finding flows
# through the real SecurityAnalysisService without mocking it.
_FILE_WITH_HARDCODED_SECRET = 'SECRET_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n'
_PATCH_FOR_SECRET_FILE = '@@ -0,0 +1,1 @@\n+SECRET_KEY = "abcdefghijklmnopqrstuvwxyz123456"'

# Triggers performance_service's REQUESTS_NO_TIMEOUT rule - pure regex, no
# scanner/AI dependency, so this is deterministic too.
_FILE_WITH_PERFORMANCE_ISSUE = 'resp = requests.get("https://example.com")\n'
_PATCH_FOR_PERFORMANCE_FILE = '@@ -0,0 +1,1 @@\n+resp = requests.get("https://example.com")'


@override_settings(GITHUB_MAX_FILE_SIZE_BYTES=500_000)
class PRAnalysisServiceTests(TestCase):
    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))
        self.pr_analysis = make_pr_analysis(self.repository)

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_analyzes_supported_file_and_persists_file_analysis(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'app.py', 'status': 'modified', 'patch': _PATCH_FOR_CLEAN_FILE},
        ]
        mock_client_cls.return_value.get_file_content.return_value = _CLEAN_PYTHON_FILE

        results = PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        self.assertEqual(len(results), 1)
        file_analysis, patch = results[0]
        self.assertEqual(file_analysis.file_path, 'app.py')
        self.assertEqual(file_analysis.language, 'Python')
        self.assertEqual(patch, _PATCH_FOR_CLEAN_FILE)

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_skipped_files_are_not_fetched_or_persisted(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'package-lock.json', 'status': 'modified', 'patch': '@@ ... @@'},
            {'filename': 'image.png', 'status': 'added', 'patch': None},
            {'filename': 'old_module.py', 'status': 'removed', 'patch': None},
        ]

        results = PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        self.assertEqual(results, [])
        mock_client_cls.return_value.get_file_content.assert_not_called()

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_file_fetch_failure_is_skipped_not_fatal(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'broken.py', 'status': 'modified', 'patch': '@@ -1,1 +1,1 @@\n+x = 1'},
            {'filename': 'app.py', 'status': 'modified', 'patch': _PATCH_FOR_CLEAN_FILE},
        ]
        mock_client_cls.return_value.get_file_content.side_effect = [
            GitHubAPIError('not found'), _CLEAN_PYTHON_FILE,
        ]

        results = PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].file_path, 'app.py')

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_oversized_file_is_skipped(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'huge.py', 'status': 'modified', 'patch': '@@ -0,0 +1,1 @@\n+x = 1'},
        ]
        mock_client_cls.return_value.get_file_content.return_value = 'x = 1\n' * 1000

        with override_settings(GITHUB_MAX_FILE_SIZE_BYTES=10):
            results = PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        self.assertEqual(results, [])

    @patch('analyses.services.ai_security_service.generate_text')
    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_real_security_service_detects_hardcoded_secret(self, mock_client_cls, mock_generate_text):
        # Bandit (B105 hardcoded_password) and CustomRulesScanner (hardcoded_secret)
        # both legitimately fire on this line under different vulnerability_type
        # keys, so dedup doesn't collapse them - real end-to-end scanner behavior,
        # only the AI enrichment call is mocked to avoid a real network call.
        mock_generate_text.return_value = '[]'
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'settings.py', 'status': 'modified', 'patch': _PATCH_FOR_SECRET_FILE},
        ]
        mock_client_cls.return_value.get_file_content.return_value = _FILE_WITH_HARDCODED_SECRET

        results = PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        file_analysis, _patch = results[0]
        security_issues = [issue for issue in file_analysis.issues if issue['source'] == 'security']
        self.assertEqual(len(security_issues), 2)
        types = {issue['type'] for issue in security_issues}
        self.assertIn('hardcoded_secret', types)
        critical_issue = next(issue for issue in security_issues if issue['type'] == 'hardcoded_secret')
        self.assertEqual(critical_issue['severity'], 'critical')

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_performance_service_findings_flow_through(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'app.py', 'status': 'modified', 'patch': _PATCH_FOR_PERFORMANCE_FILE},
        ]
        mock_client_cls.return_value.get_file_content.return_value = _FILE_WITH_PERFORMANCE_ISSUE

        results = PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        file_analysis, _patch = results[0]
        performance_issues = [issue for issue in file_analysis.issues if issue['source'] == 'performance']
        self.assertEqual(len(performance_issues), 1)
        self.assertEqual(performance_issues[0]['type'], 'requests_no_timeout')
        self.assertEqual(performance_issues[0]['severity'], 'medium')

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_updates_pr_analysis_score_summary_and_status(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'app.py', 'status': 'modified', 'patch': _PATCH_FOR_CLEAN_FILE},
        ]
        mock_client_cls.return_value.get_file_content.return_value = _CLEAN_PYTHON_FILE

        PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        self.pr_analysis.refresh_from_db()
        self.assertEqual(self.pr_analysis.status, PullRequestAnalysis.Status.COMPLETED)
        self.assertIsNotNone(self.pr_analysis.overall_score)
        self.assertIn('No issues found', self.pr_analysis.summary)

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_no_supported_files_yields_explanatory_summary(self, mock_client_cls):
        mock_client_cls.return_value.list_pull_request_files.return_value = [
            {'filename': 'README.md', 'status': 'modified', 'patch': '@@ -1,1 +1,1 @@\n+hi'},
        ]

        PRAnalysisService().analyze(self.pr_analysis, 'access-token')

        self.pr_analysis.refresh_from_db()
        self.assertEqual(self.pr_analysis.overall_score, None)
        self.assertIn('No supported files', self.pr_analysis.summary)


@override_settings(GITHUB_MAX_FILE_SIZE_BYTES=500_000)
class AnalyzeFileWithContextTests(TestCase):
    """analyze_file_with_context - like analyze_file_by_path, plus the file's
    direct dependency-graph neighbors. Uses non-Python paths throughout so the
    Python-only settings.py lookup (_find_settings_source) never kicks in and
    doesn't need its own GitHubClient mocking here."""

    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_no_index_falls_back_to_primary_file_only(self, mock_client_cls):
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertFalse(result['skipped'])
        self.assertEqual(result['related'], [])
        mock_client_cls.return_value.get_file_content.assert_called_once()

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_index_not_completed_falls_back_to_primary_file_only(self, mock_client_cls):
        RepositoryIndex.objects.create(repository=self.repository, status=RepositoryIndex.Status.RUNNING)
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(result['related'], [])

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_analyzes_direct_import_and_importer_neighbors(self, mock_client_cls):
        index = RepositoryIndex.objects.create(repository=self.repository, status=RepositoryIndex.Status.COMPLETED)
        RepositoryFileNode.objects.create(
            index=index, path='app.js', language='JavaScript', imports=['utils.js'], imported_by=['main.js'],
        )
        mock_client_cls.return_value.get_file_content.side_effect = [
            'const x = 1;\n',              # primary: app.js
            'export const y = 2;\n',       # related: utils.js (imports)
            'import app from "./app";\n',  # related: main.js (imported_by)
        ]

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), 2)
        by_path = {r['path']: r for r in result['related']}
        self.assertEqual(by_path['utils.js']['relation'], 'imports')
        self.assertEqual(by_path['main.js']['relation'], 'imported_by')
        self.assertEqual(mock_client_cls.return_value.get_file_content.call_count, 3)

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_skip_eligible_neighbor_is_left_out(self, mock_client_cls):
        index = RepositoryIndex.objects.create(repository=self.repository, status=RepositoryIndex.Status.COMPLETED)
        RepositoryFileNode.objects.create(
            index=index, path='app.js', language='JavaScript', imports=['package-lock.json'], imported_by=[],
        )
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(result['related'], [])
        mock_client_cls.return_value.get_file_content.assert_called_once()  # only the primary file

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_neighbor_fetch_failure_is_skipped_not_fatal(self, mock_client_cls):
        index = RepositoryIndex.objects.create(repository=self.repository, status=RepositoryIndex.Status.COMPLETED)
        RepositoryFileNode.objects.create(
            index=index, path='app.js', language='JavaScript', imports=['utils.js'], imported_by=[],
        )
        mock_client_cls.return_value.get_file_content.side_effect = ['const x = 1;\n', GitHubAPIError('not found')]

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(result['related'], [])

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_neighbors_capped_per_relation(self, mock_client_cls):
        index = RepositoryIndex.objects.create(repository=self.repository, status=RepositoryIndex.Status.COMPLETED)
        RepositoryFileNode.objects.create(
            index=index, path='app.js', language='JavaScript',
            imports=[f'mod{i}.js' for i in range(5)], imported_by=[],
        )
        mock_client_cls.return_value.get_file_content.return_value = 'const x = 1;\n'

        result = PRAnalysisService().analyze_file_with_context(self.repository, 'app.js', 'token')

        self.assertEqual(len(result['related']), MAX_CONTEXT_RELATED_FILES)

    @patch('github_integration.services.pr_analysis_service.GitHubClient')
    def test_skipped_primary_file_returns_no_related_and_no_fetch(self, mock_client_cls):
        result = PRAnalysisService().analyze_file_with_context(self.repository, 'yarn.lock', 'token')

        self.assertTrue(result['skipped'])
        self.assertEqual(result['related'], [])
        mock_client_cls.return_value.get_file_content.assert_not_called()
