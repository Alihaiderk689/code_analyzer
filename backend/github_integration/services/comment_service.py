"""Formats FileAnalysis issues into GitHub PR review comments and posts them
in a single review (like a human reviewer leaving line comments plus an
overall summary) via GitHubClient.create_review.

Only issues that land on a line GitHub will actually let us comment on (per
patch_parser) become inline comments; everything else - including any excess
past MAX_INLINE_COMMENTS, so one huge PR can't spam dozens of individual
comments - is folded into the review's summary body instead, matching "If
line mapping is not available, post a single summary review comment."
"""
from __future__ import annotations

import logging

from ..models import FileAnalysis, PullRequestAnalysis
from .github_client import GitHubAPIError, GitHubClient
from .patch_parser import parse_commentable_lines

logger = logging.getLogger(__name__)

_SEVERITY_LABELS = {
    'critical': '🔴 Critical Severity',
    'high': '⚠️ High Severity',
    'medium': '🟡 Medium Severity',
    'low': '🔵 Low Severity',
}

# GitHub's review UI degrades badly with dozens of inline comments on one PR,
# and past this point it stops being useful feedback for the author anyway.
MAX_INLINE_COMMENTS = 25


def _format_issue_title(issue: dict) -> str:
    label = _SEVERITY_LABELS.get(issue['severity'], issue['severity'].title())
    issue_name = issue['type'].replace('_', ' ').title()
    return f'{label} — {issue_name}'


def _format_comment_body(issue: dict) -> str:
    parts = [f"**{_format_issue_title(issue)}**", '', issue['message']]
    if issue.get('explanation') and issue['explanation'] != issue['message']:
        parts += ['', issue['explanation']]
    if issue.get('remediation'):
        parts += ['', f"**Suggested fix:** {issue['remediation']}"]
    return '\n'.join(parts)


class CommentService:
    def post_review(
        self, pr_analysis: PullRequestAnalysis, file_results: list[tuple[FileAnalysis, str]], access_token: str,
    ) -> None:
        owner, _, repo = pr_analysis.repository.full_name.partition('/')
        client = GitHubClient(access_token)

        inline_comments, overflow = self._build_inline_comments(file_results)
        body = self._build_summary_body(pr_analysis, overflow)

        try:
            client.create_review(
                owner, repo, pr_analysis.pull_request_number, pr_analysis.commit_sha,
                body=body, comments=inline_comments, event='COMMENT',
            )
        except GitHubAPIError:
            if not inline_comments:
                raise
            # GitHub can still reject an individual inline comment for subtler
            # reasons than patch_parser can catch - rather than losing the
            # review entirely, fall back to a summary-only one.
            logger.warning('github_comment.review_with_inline_comments_failed', exc_info=True)
            client.create_review(
                owner, repo, pr_analysis.pull_request_number, pr_analysis.commit_sha,
                body=body, comments=[], event='COMMENT',
            )

        pr_analysis.review_posted = True
        pr_analysis.save(update_fields=['review_posted', 'updated_at'])
        logger.info(
            'github_comment.review_posted', extra={
                'repository': pr_analysis.repository.full_name,
                'pull_request_number': pr_analysis.pull_request_number,
                'inline_comments': len(inline_comments),
            },
        )

    @staticmethod
    def _build_inline_comments(
        file_results: list[tuple[FileAnalysis, str]],
    ) -> tuple[list[dict], list[tuple[str, dict]]]:
        inline_comments: list[dict] = []
        overflow: list[tuple[str, dict]] = []  # (file_path, issue) for anything not inlined

        for file_analysis, patch in file_results:
            commentable_lines = parse_commentable_lines(patch)
            for issue in file_analysis.issues:
                line = issue.get('line')
                fits_in_diff = line and line in commentable_lines
                if fits_in_diff and len(inline_comments) < MAX_INLINE_COMMENTS:
                    inline_comments.append({
                        'path': file_analysis.file_path,
                        'line': line,
                        'side': 'RIGHT',
                        'body': _format_comment_body(issue),
                    })
                else:
                    overflow.append((file_analysis.file_path, issue))

        return inline_comments, overflow

    @staticmethod
    def _build_summary_body(pr_analysis: PullRequestAnalysis, overflow: list[tuple[str, dict]]) -> str:
        lines = ['## 🤖 AI Code Review', '']
        if pr_analysis.overall_score is not None:
            lines.append(f'**Overall score:** {pr_analysis.overall_score}/100')
        lines.append(pr_analysis.summary or 'No issues found.')

        if overflow:
            lines += ['', "**Additional findings** (couldn't be attached to a specific diff line):", '']
            for file_path, issue in overflow:
                line_number = issue.get('line')
                location = f'{file_path}:{line_number}' if line_number else file_path
                lines.append(f"- `{location}` — {_format_issue_title(issue)}: {issue['message']}")

        return '\n'.join(lines)
