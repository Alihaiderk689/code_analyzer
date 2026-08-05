"""Builds a lightweight, best-effort dependency graph for a monitored repo:
which files import which, plus a short content summary per file. Powers
pr_analysis_service._build_repo_context, which hands a file's immediate
neighbors to the AI when analyzing it on demand, instead of the file in
total isolation.

Deliberately scoped to Python/JavaScript/TypeScript/JSX/TSX only - the two
ecosystems import_parser.py knows how to extract imports from (see its
docstring for why other supported-for-analysis languages aren't included
here). Bounded by GITHUB_MAX_INDEXED_FILES so indexing a huge repo can't
exhaust the GitHub API rate limit or run forever - see build().
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.utils import timezone

from ..models import GitHubRepository, RepositoryFileNode, RepositoryIndex
from .github_client import GitHubAPIError, GitHubClient
from .import_parser import extract_imports, resolve_import
from .pr_analysis_service import classify_path

logger = logging.getLogger(__name__)

# Only languages import_parser.py can actually extract imports from - see its
# module docstring. Indexing any other classify_path-supported language would
# only ever produce empty imports/imported_by, so it isn't worth the extra
# GitHub API call per file.
_INDEXABLE_LANGUAGES = {'Python', 'JavaScript', 'TypeScript'}

_SUMMARY_MAX_LINES = 60
_SUMMARY_MAX_CHARS = 2000


def _summarize(content: str) -> str:
    lines = content.splitlines()
    truncated_lines = len(lines) > _SUMMARY_MAX_LINES
    excerpt = '\n'.join(lines[:_SUMMARY_MAX_LINES])
    if len(excerpt) > _SUMMARY_MAX_CHARS:
        excerpt = excerpt[:_SUMMARY_MAX_CHARS]
        truncated_lines = True
    if truncated_lines:
        excerpt += '\n... (truncated)'
    return excerpt


class RepositoryIndexService:
    def build(self, repository: GitHubRepository) -> RepositoryIndex:
        index, _created = RepositoryIndex.objects.get_or_create(repository=repository)
        index.status = RepositoryIndex.Status.RUNNING
        index.error = ''
        index.save(update_fields=['status', 'error', 'updated_at'])

        try:
            self._build(repository, index)
        except GitHubAPIError:
            raise  # let the Celery task's differentiated handling decide retry vs. fail
        except Exception:
            logger.exception('repository_index.build_failed', extra={'repository': repository.full_name})
            index.status = RepositoryIndex.Status.FAILED
            index.error = 'Unexpected error while building the repository index.'
            index.save(update_fields=['status', 'error', 'updated_at'])
            raise
        return index

    def _build(self, repository: GitHubRepository, index: RepositoryIndex) -> None:
        owner, _, repo = repository.full_name.partition('/')
        client = GitHubClient(repository.integration.get_access_token())

        tree = client.get_repository_tree(owner, repo, repository.default_branch)
        entries = [e for e in tree['entries'] if e['type'] == 'file']
        all_paths = {e['path'] for e in entries}

        candidates = [
            e['path'] for e in entries
            if self._is_indexable(e)
        ]
        capped = len(candidates) > settings.GITHUB_MAX_INDEXED_FILES
        if capped:
            candidates = candidates[:settings.GITHUB_MAX_INDEXED_FILES]

        file_data = {}  # path -> {'language', 'summary', 'imports': set[str]}
        for path in candidates:
            language, skip_reason = classify_path(path)
            if skip_reason:
                continue
            try:
                content = client.get_file_content(owner, repo, path, repository.default_branch)
            except GitHubAPIError:
                # One unreadable file (permissions, submodule, race with a
                # force-push) shouldn't fail the whole index.
                logger.warning('repository_index.file_fetch_failed', exc_info=True, extra={'path': path})
                continue

            raw_imports = extract_imports(content, language)
            resolved = {resolve_import(raw, path, all_paths, language) for raw in raw_imports}
            resolved.discard(None)
            resolved.discard(path)  # a self-import isn't a useful edge

            file_data[path] = {
                'language': language,
                'summary': _summarize(content),
                'imports': resolved,
            }

        imported_by = defaultdict(set)
        for path, data in file_data.items():
            for target in data['imports']:
                imported_by[target].add(path)

        RepositoryFileNode.objects.filter(index=index).delete()
        RepositoryFileNode.objects.bulk_create([
            RepositoryFileNode(
                index=index, path=path, language=data['language'], summary=data['summary'],
                imports=sorted(data['imports']), imported_by=sorted(imported_by.get(path, [])),
            )
            for path, data in file_data.items()
        ])

        index.files_total = len(entries)
        index.files_indexed = len(file_data)
        index.truncated = tree['truncated'] or capped
        index.status = RepositoryIndex.Status.COMPLETED
        index.indexed_at = timezone.now()
        index.save(update_fields=[
            'files_total', 'files_indexed', 'truncated', 'status', 'indexed_at', 'updated_at',
        ])

        logger.info(
            'repository_index.built',
            extra={
                'repository': repository.full_name, 'files_total': index.files_total,
                'files_indexed': index.files_indexed, 'truncated': index.truncated,
            },
        )

    @staticmethod
    def _is_indexable(entry: dict) -> bool:
        language, skip_reason = classify_path(entry['path'])
        if skip_reason or language not in _INDEXABLE_LANGUAGES:
            return False
        size = entry.get('size') or 0
        return size <= settings.GITHUB_MAX_FILE_SIZE_BYTES
