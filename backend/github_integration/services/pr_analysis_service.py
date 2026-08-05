"""Analyzes a pull request's changed files by calling the *existing* analysis
services this project already has - analyses.engine.analyze_code for general
code-quality issues, analyses.services.security_service.SecurityAnalysisService
for security findings (Bandit + custom rules + AI explanation/remediation) -
plus this app's own lightweight performance-smell scanner (performance_service),
since no performance-detection category exists anywhere else in the project.

This module only orchestrates: fetch changed files from GitHub, filter out
what shouldn't be analyzed, route the rest through those existing services,
and normalize their differently-shaped output into one issue list per file.
No quality/security analysis logic - static or AI - is reimplemented here.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from django.conf import settings

from analyses.engine import analyze_code
from analyses.services.report_generator import SEVERITY_PENALTIES, STARTING_SCORE
from analyses.services.security_service import SecurityAnalysisService

from ..models import FileAnalysis, PullRequestAnalysis, RepositoryIndex
from .github_client import GitHubAPIError, GitHubClient
from .performance_service import find_performance_issues

logger = logging.getLogger(__name__)

# How many of a file's imports/importers get pulled into repo_context - bounds
# prompt size regardless of how connected a file is in a large repo.
MAX_RELATED_FILES = 5

# How many of a file's imports/importers get actually fetched-and-analyzed by
# analyze_file_with_context, per relation (imports / imported_by) - smaller
# than MAX_RELATED_FILES above since each one costs a real GitHub fetch and
# potentially a Groq call (security enrichment), not just a few lines of
# prompt text.
MAX_CONTEXT_RELATED_FILES = 3

# Matches the languages this feature is required to support. Deliberately its
# own map rather than reusing analyses.engine.LANGUAGE_BY_EXTENSION - that one
# covers a broader set (Ruby, Kotlin, Swift, HTML, CSS, ...) intended for the
# manual upload flow, and is missing two languages required here (Rust, C#).
SUPPORTED_EXTENSIONS = {
    '.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript', '.ts': 'TypeScript', '.tsx': 'TypeScript',
    '.java': 'Java', '.cs': 'C#', '.cpp': 'C++', '.cc': 'C++', '.h': 'C++', '.hpp': 'C++',
    '.go': 'Go', '.rs': 'Rust', '.php': 'PHP',
}

LOCK_FILENAMES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'Pipfile.lock', 'poetry.lock',
    'Cargo.lock', 'go.sum', 'composer.lock', 'Gemfile.lock', 'composer.json.lock',
}

# Path/filename heuristics for generated/vendored code - not exhaustive (there's
# no fully reliable way to detect "generated" without a `@generated` marker in
# the content itself), but catches the overwhelmingly common cases.
_GENERATED_PATH_MARKERS = ('/dist/', '/build/', '/vendor/', '/node_modules/', '/.next/')


class FileSkipReason:
    BINARY = 'binary'
    LOCK_FILE = 'lock_file'
    GENERATED = 'generated'
    UNSUPPORTED_LANGUAGE = 'unsupported_language'
    TOO_LARGE = 'too_large'
    REMOVED = 'removed'
    FETCH_FAILED = 'fetch_failed'


def classify_path(path: str) -> tuple[Optional[str], Optional[str]]:
    """The part of classification that depends only on the path, not on
    PR-diff-specific metadata (status/whether GitHub gave us a patch) - so
    on-demand single-file analysis (fetched directly by path, not from a PR
    diff) can reuse the same lock-file/generated/language rules. Public (no
    leading underscore) because the file-check view also needs it, to
    classify a path *before* the daily-quota gate - see
    RepositoryFileAnalyzeView.post."""
    filename = path.rsplit('/', 1)[-1]
    if filename in LOCK_FILENAMES:
        return None, FileSkipReason.LOCK_FILE
    if filename.endswith('.min.js') or any(marker in f'/{path}' for marker in _GENERATED_PATH_MARKERS):
        return None, FileSkipReason.GENERATED

    ext = os.path.splitext(filename)[1].lower()
    language = SUPPORTED_EXTENSIONS.get(ext)
    if not language:
        return None, FileSkipReason.UNSUPPORTED_LANGUAGE
    return language, None


def _classify_file(path: str, status: str, has_patch: bool) -> tuple[Optional[str], Optional[str]]:
    """Returns (language, skip_reason) - exactly one of the two is set. Only
    looks at metadata GitHub already gave us in the PR-files response, so this
    never needs a network call."""
    if status == 'removed':
        return None, FileSkipReason.REMOVED
    if not has_patch:
        return None, FileSkipReason.BINARY
    return classify_path(path)


def _severity_from_penalty(penalty: int) -> str:
    if penalty >= 30:
        return 'critical'
    if penalty >= 15:
        return 'high'
    if penalty >= 5:
        return 'medium'
    return 'low'


def _find_settings_source(client: GitHubClient, owner: str, repo: str, ref: str) -> str:
    """Best-effort fetch of the analyzed repo's Django settings, so the
    missing-auth heuristic (see custom_rules_service.py) can tell whether a
    view relying on DRF's global DEFAULT_PERMISSION_CLASSES is actually
    protected, instead of judging every file in total isolation. Reuses the
    same recursive tree call the file-browser feature is built on, rather
    than guessing at a project-specific folder name (which varies per repo -
    only the *filename* convention is reasonably universal). Silently returns
    '' (same as "unknown") on any failure - this only ever refines the
    existing heuristic, never gates it."""
    try:
        tree = client.get_repository_tree(owner, repo, ref)['entries']
    except GitHubAPIError:
        return ''
    candidates = sorted(
        (entry['path'] for entry in tree if entry['type'] == 'file' and entry['path'].endswith('settings.py')),
        key=len,
    )
    if not candidates:
        return ''
    try:
        return client.get_file_content(owner, repo, candidates[0], ref)
    except GitHubAPIError:
        return ''


def _build_repo_context(repository: 'GitHubRepository', path: str) -> str:
    """Best-effort: hands the AI a file's immediate neighbors (what it
    imports, what imports it) from the repo's dependency graph (see
    repo_index_service.py), if one exists. Silently returns '' if there's no
    index yet, it's still building/failed, or this file wasn't indexed (e.g.
    an unsupported language or over GITHUB_MAX_INDEXED_FILES) - this only
    ever enriches an analysis, never gates it."""
    try:
        index = repository.index
    except RepositoryIndex.DoesNotExist:
        return ''
    if index.status != RepositoryIndex.Status.COMPLETED:
        return ''

    node = index.files.filter(path=path).first()
    if node is None:
        return ''

    related_paths = [*node.imports[:MAX_RELATED_FILES], *node.imported_by[:MAX_RELATED_FILES]]
    summaries_by_path = dict(index.files.filter(path__in=related_paths).values_list('path', 'summary'))

    sections = []
    if node.imports:
        lines = [
            f'--- {p} ---\n{summaries_by_path[p]}' if p in summaries_by_path else f'--- {p} --- (not indexed)'
            for p in node.imports[:MAX_RELATED_FILES]
        ]
        sections.append('Files this file imports:\n' + '\n\n'.join(lines))
    if node.imported_by:
        lines = [
            f'--- {p} ---\n{summaries_by_path[p]}' if p in summaries_by_path else f'--- {p} --- (not indexed)'
            for p in node.imported_by[:MAX_RELATED_FILES]
        ]
        sections.append('Files that import this file:\n' + '\n\n'.join(lines))

    if not sections:
        return ''
    return 'Repository context - other files related to this one:\n\n' + '\n\n'.join(sections)


def _related_paths(repository: 'GitHubRepository', path: str) -> list[tuple[str, str]]:
    """[(related_path, 'imports'|'imported_by'), ...] for a file's direct
    dependency-graph neighbors, deduplicated and capped at
    MAX_CONTEXT_RELATED_FILES per relation - the set analyze_file_with_context
    actually fetches and analyzes. Returns [] the same way _build_repo_context
    does (no index yet, still building/failed, or this file wasn't indexed) -
    this only ever adds to a context check, never blocks it."""
    try:
        index = repository.index
    except RepositoryIndex.DoesNotExist:
        return []
    if index.status != RepositoryIndex.Status.COMPLETED:
        return []

    node = index.files.filter(path=path).first()
    if node is None:
        return []

    seen = {path}
    related: list[tuple[str, str]] = []
    for p in node.imports[:MAX_CONTEXT_RELATED_FILES]:
        if p not in seen:
            seen.add(p)
            related.append((p, 'imports'))
    for p in node.imported_by[:MAX_CONTEXT_RELATED_FILES]:
        if p not in seen:
            seen.add(p)
            related.append((p, 'imported_by'))
    return related


def _analyze_file_content(content: str, language: str, settings_source: str = '') -> list[dict]:
    """Normalizes analyses.engine's issues and SecurityAnalysisService's
    vulnerabilities into one common shape - see FileAnalysis.issues docstring
    in models.py for the exact fields."""
    issues = []

    quality_result = analyze_code(content, language)
    for issue in quality_result['issues']:
        issues.append({
            'source': 'quality',
            'type': issue['type'],
            'severity': _severity_from_penalty(_quality_penalty(issue['type'])),
            'line': issue.get('line'),
            'message': issue['message'],
            'explanation': None,
            'remediation': None,
        })

    # SecurityAnalysisService already gates Bandit to Python internally and
    # always runs the (language-agnostic) custom-rules scanner; if that finds
    # nothing, AISecurityService short-circuits before ever calling the LLM -
    # so this never pays for a wasted AI call on a clean/non-Python file.
    security_report = SecurityAnalysisService().analyze(content, language, settings_source=settings_source)
    for vuln in security_report['vulnerabilities']:
        issues.append({
            'source': 'security',
            'type': vuln['vulnerability_type'],
            'severity': vuln['severity'],
            'line': vuln['line_number'],
            'message': vuln['title'],
            'explanation': vuln['explanation'],
            'remediation': vuln['remediation'],
        })

    issues.extend(find_performance_issues(content))

    return issues


def _quality_penalty(issue_type: str) -> int:
    from analyses.engine import ISSUE_PENALTIES
    return ISSUE_PENALTIES.get(issue_type, 2)


def _score_for_issues(issues: list[dict]) -> float:
    penalty = sum(SEVERITY_PENALTIES.get(issue['severity'], 0) for issue in issues)
    return max(0.0, STARTING_SCORE - penalty)


class PRAnalysisService:
    def analyze(self, pr_analysis: PullRequestAnalysis, access_token: str) -> list[tuple[FileAnalysis, str]]:
        """Fetches the PR's changed files, analyzes the supported ones, persists
        a FileAnalysis per analyzed file, and updates pr_analysis's own score/
        summary/status in place. Returns [(FileAnalysis, patch_text), ...] -
        the patch text is what comment_service needs to know which lines are
        valid inline-comment targets."""
        owner, _, repo = pr_analysis.repository.full_name.partition('/')
        client = GitHubClient(access_token)

        changed_files = client.list_pull_request_files(owner, repo, pr_analysis.pull_request_number)

        # Fetched at most once per PR review, lazily - the settings lookup costs
        # a couple of extra API calls (see _find_settings_source) that are only
        # worth paying if this PR actually touches Python; None means "not
        # looked up yet", distinct from '' meaning "looked up, nothing found".
        settings_source = None

        results: list[tuple[FileAnalysis, str]] = []
        for file_info in changed_files:
            path = file_info['filename']
            language, skip_reason = _classify_file(
                path, file_info.get('status', ''), bool(file_info.get('patch')),
            )
            if skip_reason:
                logger.info('github_pr_analysis.file_skipped', extra={'path': path, 'reason': skip_reason})
                continue

            try:
                content = client.get_file_content(owner, repo, path, pr_analysis.commit_sha)
            except GitHubAPIError:
                logger.warning('github_pr_analysis.file_fetch_failed', exc_info=True, extra={'path': path})
                continue

            if len(content.encode('utf-8')) > settings.GITHUB_MAX_FILE_SIZE_BYTES:
                logger.info('github_pr_analysis.file_skipped', extra={'path': path, 'reason': FileSkipReason.TOO_LARGE})
                continue

            if language == 'Python' and settings_source is None:
                settings_source = _find_settings_source(client, owner, repo, pr_analysis.commit_sha)

            issues = _analyze_file_content(content, language, settings_source or '')
            file_analysis = FileAnalysis.objects.create(
                pull_request_analysis=pr_analysis,
                file_path=path,
                language=language,
                issues=issues,
                score=_score_for_issues(issues),
            )
            results.append((file_analysis, file_info.get('patch', '')))

        pr_analysis.overall_score = self._overall_score(results)
        pr_analysis.summary = self._build_summary(results)
        pr_analysis.status = PullRequestAnalysis.Status.COMPLETED
        pr_analysis.save(update_fields=['overall_score', 'summary', 'status', 'updated_at'])

        logger.info(
            'github_pr_analysis.completed', extra={
                'repository': pr_analysis.repository.full_name,
                'pull_request_number': pr_analysis.pull_request_number,
                'files_analyzed': len(results),
                'overall_score': pr_analysis.overall_score,
            },
        )
        return results

    def analyze_file_by_path(self, repository: 'GitHubRepository', path: str, access_token: str) -> dict:
        """On-demand analysis of one file at the HEAD of the repo's default
        branch - not tied to a PR, not automatic. Exactly one GitHub content
        fetch and the same quality/security/performance pipeline PR reviews
        use. Caller (the view) is responsible for the daily quota check -
        this method always does the real work when called."""
        language, skip_reason = classify_path(path)
        if skip_reason:
            return {'path': path, 'language': None, 'skipped': True, 'skip_reason': skip_reason, 'issues': [], 'score': None}

        owner, _, repo = repository.full_name.partition('/')
        client = GitHubClient(access_token)
        content = client.get_file_content(owner, repo, path, repository.default_branch)

        if len(content.encode('utf-8')) > settings.GITHUB_MAX_FILE_SIZE_BYTES:
            return {
                'path': path, 'language': language, 'skipped': True,
                'skip_reason': FileSkipReason.TOO_LARGE, 'issues': [], 'score': None,
            }

        settings_source = _find_settings_source(client, owner, repo, repository.default_branch) if language == 'Python' else ''
        issues = _analyze_file_content(content, language, settings_source)
        return {
            'path': path, 'language': language, 'skipped': False, 'content': content,
            'skip_reason': None, 'issues': issues, 'score': _score_for_issues(issues),
            'repo_context': _build_repo_context(repository, path),
        }

    def analyze_file_with_context(self, repository: 'GitHubRepository', path: str, access_token: str) -> dict:
        """Like analyze_file_by_path, plus the file's direct dependency-graph
        neighbors (what it imports, what imports it - see _related_paths),
        each run through the same quality/security/performance pipeline, so a
        change's impact on related files is visible instead of just the one
        file in isolation. Falls back to the primary file alone if there's no
        completed index yet or it has no recorded neighbors. Caller is
        responsible for the daily quota check (see RepositoryContextCheck) -
        this method always does the real work when called."""
        primary = self.analyze_file_by_path(repository, path, access_token)
        if primary['skipped']:
            return {**primary, 'related': []}

        owner, _, repo = repository.full_name.partition('/')
        client = GitHubClient(access_token)
        settings_source = None  # lazy, only fetched if a related Python file actually needs it

        related = []
        for related_path, relation in _related_paths(repository, path):
            related_language, skip_reason = classify_path(related_path)
            if skip_reason:
                continue

            try:
                content = client.get_file_content(owner, repo, related_path, repository.default_branch)
            except GitHubAPIError:
                logger.warning(
                    'github_context_check.related_fetch_failed', exc_info=True, extra={'path': related_path},
                )
                continue

            if len(content.encode('utf-8')) > settings.GITHUB_MAX_FILE_SIZE_BYTES:
                continue

            if related_language == 'Python' and settings_source is None:
                settings_source = _find_settings_source(client, owner, repo, repository.default_branch)

            issues = _analyze_file_content(content, related_language, settings_source or '')
            related.append({
                'path': related_path, 'language': related_language, 'relation': relation,
                'issues': issues, 'score': _score_for_issues(issues),
            })

        return {**primary, 'related': related}

    @staticmethod
    def _overall_score(results: list[tuple[FileAnalysis, str]]) -> Optional[float]:
        scores = [file_analysis.score for file_analysis, _patch in results if file_analysis.score is not None]
        if not scores:
            return None
        return round(sum(scores) / len(scores), 1)

    @staticmethod
    def _build_summary(results: list[tuple[FileAnalysis, str]]) -> str:
        if not results:
            return 'No supported files were changed in this pull request.'

        total_issues = sum(len(file_analysis.issues) for file_analysis, _patch in results)
        critical = sum(
            1 for file_analysis, _patch in results for issue in file_analysis.issues
            if issue['severity'] == 'critical'
        )
        if total_issues == 0:
            return f'Analyzed {len(results)} file(s). No issues found.'

        summary = f'Analyzed {len(results)} file(s), found {total_issues} issue(s)'
        if critical:
            summary += f', including {critical} critical'
        return summary + '.'
