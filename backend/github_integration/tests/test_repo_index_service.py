from unittest.mock import patch

from django.test import TestCase, override_settings

from ..models import RepositoryFileNode, RepositoryIndex
from ..services.github_client import GitHubAPIError
from ..services.repo_index_service import RepositoryIndexService
from .factories import TEST_ENCRYPTION_KEY, make_integration, make_repository, make_user

_SETTINGS = dict(GITHUB_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)


def _tree(entries, truncated=False):
    return {'entries': entries, 'truncated': truncated}


def _entry(path, size=100):
    return {'path': path, 'type': 'file', 'size': size}


@override_settings(**_SETTINGS)
class RepositoryIndexServiceTests(TestCase):
    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))

    @patch('github_integration.services.repo_index_service.GitHubClient')
    def test_builds_graph_with_import_edges(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([
            _entry('app.py'), _entry('utils.py'),
        ])
        mock_client_cls.return_value.get_file_content.side_effect = lambda owner, repo, path, ref: {
            'app.py': 'from utils import helper\n\nhelper()\n',
            'utils.py': 'def helper():\n    return 1\n',
        }[path]

        index = RepositoryIndexService().build(self.repository)

        self.assertEqual(index.status, RepositoryIndex.Status.COMPLETED)
        self.assertEqual(index.files_indexed, 2)
        self.assertFalse(index.truncated)

        app_node = RepositoryFileNode.objects.get(index=index, path='app.py')
        utils_node = RepositoryFileNode.objects.get(index=index, path='utils.py')
        self.assertEqual(app_node.imports, ['utils.py'])
        self.assertEqual(utils_node.imported_by, ['app.py'])
        self.assertIn('def helper', utils_node.summary)

    @patch('github_integration.services.repo_index_service.GitHubClient')
    def test_skips_non_indexable_language(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([_entry('main.go')])

        index = RepositoryIndexService().build(self.repository)

        self.assertEqual(index.files_indexed, 0)
        mock_client_cls.return_value.get_file_content.assert_not_called()

    @patch('github_integration.services.repo_index_service.GitHubClient')
    @override_settings(GITHUB_MAX_FILE_SIZE_BYTES=10, **_SETTINGS)
    def test_skips_files_over_size_cap(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([_entry('big.py', size=999)])

        index = RepositoryIndexService().build(self.repository)

        self.assertEqual(index.files_indexed, 0)
        mock_client_cls.return_value.get_file_content.assert_not_called()

    @patch('github_integration.services.repo_index_service.GitHubClient')
    @override_settings(GITHUB_MAX_INDEXED_FILES=1, **_SETTINGS)
    def test_caps_at_max_indexed_files_and_marks_truncated(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([
            _entry('a.py'), _entry('b.py'),
        ])
        mock_client_cls.return_value.get_file_content.return_value = 'x = 1\n'

        index = RepositoryIndexService().build(self.repository)

        self.assertEqual(index.files_indexed, 1)
        self.assertTrue(index.truncated)

    @patch('github_integration.services.repo_index_service.GitHubClient')
    def test_marks_truncated_when_github_tree_itself_truncated(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([_entry('a.py')], truncated=True)
        mock_client_cls.return_value.get_file_content.return_value = 'x = 1\n'

        index = RepositoryIndexService().build(self.repository)

        self.assertTrue(index.truncated)

    @patch('github_integration.services.repo_index_service.GitHubClient')
    def test_one_unreadable_file_does_not_fail_the_whole_build(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([
            _entry('good.py'), _entry('bad.py'),
        ])

        def fetch(owner, repo, path, ref):
            if path == 'bad.py':
                raise GitHubAPIError('gone')
            return 'x = 1\n'

        mock_client_cls.return_value.get_file_content.side_effect = fetch

        index = RepositoryIndexService().build(self.repository)

        self.assertEqual(index.status, RepositoryIndex.Status.COMPLETED)
        self.assertEqual(index.files_indexed, 1)
        self.assertTrue(RepositoryFileNode.objects.filter(index=index, path='good.py').exists())

    @patch('github_integration.services.repo_index_service.GitHubClient')
    def test_rebuild_replaces_stale_nodes(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.return_value = _tree([_entry('a.py')])
        mock_client_cls.return_value.get_file_content.return_value = 'x = 1\n'
        first_index = RepositoryIndexService().build(self.repository)
        self.assertEqual(RepositoryFileNode.objects.filter(index=first_index).count(), 1)

        mock_client_cls.return_value.get_repository_tree.return_value = _tree([_entry('b.py')])
        second_index = RepositoryIndexService().build(self.repository)

        self.assertEqual(first_index.pk, second_index.pk)  # OneToOne - same row, rebuilt in place
        self.assertEqual(
            list(RepositoryFileNode.objects.filter(index=second_index).values_list('path', flat=True)), ['b.py'],
        )

    @patch('github_integration.services.repo_index_service.GitHubClient')
    def test_propagates_github_api_error_for_task_level_retry_handling(self, mock_client_cls):
        mock_client_cls.return_value.get_repository_tree.side_effect = GitHubAPIError('down')
        with self.assertRaises(GitHubAPIError):
            RepositoryIndexService().build(self.repository)
