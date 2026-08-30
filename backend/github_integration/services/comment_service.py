"""Formats FileAnalysis issues into GitHub PR review comments and posts them
in a single review (like a human reviewer leaving line comments plus an
overall summary) via GitHubClient.create_review.

Only issues that land on a line GitHub will actually let us comment on (per
patch_parser) become inline comments; everything else - including any excess
past MAX_INLINE_COMMENTS, so one huge PR can't spam dozens of individual
comments - is folded into the review's summary body instead, matching "If
line mapping is not available, post a single summary review comment."

Every free-text field an issue carries (`message`, and especially the
AI-written `explanation`/`remediation` - see analyses.services.
ai_security_service) is treated as untrusted before it becomes live GitHub
markdown, via `_sanitize_for_github` below - not because the app doesn't
trust its own AI integration's *intent*, but because the model is an
external text generator whose output this app does not fully control, and
the scanner snippet that prompt was built from is itself drawn from a PR
author's own submitted code. This is the single, centralized boundary that
content crosses on its way to a real, externally-visible GitHub write -
individual callers don't each implement their own escaping.
"""
from __future__ import annotations

import logging
import re

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

# Individual comment fields, not the whole review body - GitHub's own review
# comment length limit is far higher than this, but a single finding's
# explanation/remediation has no legitimate reason to be long prose.
MAX_COMMENT_FIELD_LENGTH = 1000

_HTML_TAG_RE = re.compile(r'</?[a-zA-Z][^>\n]*>')
_MENTION_RE = re.compile(r'@(?=\w)')
_URL_RE = re.compile(r'https?://\S+')


def _sanitize_for_github(text) -> str:
    """Neutralizes exactly the constructs that would make GitHub treat this
    text as *live* content rather than inert prose - not markdown formatting
    in general, which is left alone (bold/italic/lists/code spans carry no
    "active content" risk and are worth preserving):
      - raw HTML tags, stripped outright (GitHub's renderer allows a safe
        subset of inline HTML; "none at all" is the simplest correct stance
        for content this app doesn't fully control the origin of)
      - @mentions, defanged with a zero-width space right after '@' so
        GitHub's mention-detection regex no longer matches, while the text
        still reads identically to a human
      - URLs (bare, or as a markdown link's target) wrapped in a code span,
        which GitHub does not auto-link and does not parse markdown inside -
        this also neutralizes the `(url)` half of a `[text](url)` link,
        since the URL itself is what makes a link "live"
    Returns '' for anything that isn't a non-empty string - a malformed/
    adversarial value fails closed to "nothing added" rather than being
    coerced or raising, and every caller here already treats an empty
    sanitized field the same way it already treats an absent one."""
    if not isinstance(text, str):
        return ''
    text = text.strip()
    if not text:
        return ''
    text = _HTML_TAG_RE.sub('', text)
    text = _MENTION_RE.sub('@' + chr(0x200B), text)  # zero-width space (U+200B), via chr() to keep it unambiguous in source
    text = _URL_RE.sub(lambda m: f'`{m.group(0)}`', text)
    if len(text) > MAX_COMMENT_FIELD_LENGTH:
        text = text[:MAX_COMMENT_FIELD_LENGTH].rstrip() + '…'
    return text


def _format_issue_title(issue: dict) -> str:
    severity = _sanitize_for_github(issue.get('severity', ''))
    label = _SEVERITY_LABELS.get(severity, severity.title() or 'Issue')
    issue_name = _sanitize_for_github(issue.get('type', '')).replace('_', ' ').title() or 'Unknown'
    return f'{label} — {issue_name}'


def _format_comment_body(issue: dict) -> str:
    message = _sanitize_for_github(issue.get('message', ''))
    explanation = _sanitize_for_github(issue.get('explanation'))
    remediation = _sanitize_for_github(issue.get('remediation'))

    parts = [f"**{_format_issue_title(issue)}**", '', message]
    if explanation and explanation != message:
        parts += ['', explanation]
    if remediation:
        parts += ['', f"**Suggested fix:** {remediation}"]
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
        lines.append(_sanitize_for_github(pr_analysis.summary) or 'No issues found.')

        if overflow:
            lines += ['', "**Additional findings** (couldn't be attached to a specific diff line):", '']
            for file_path, issue in overflow:
                line_number = issue.get('line')
                safe_path = _sanitize_for_github(file_path).replace('`', "'")
                location = f'{safe_path}:{line_number}' if line_number else safe_path
                message = _sanitize_for_github(issue.get('message', ''))
                lines.append(f"- `{location}` — {_format_issue_title(issue)}: {message}")

        return '\n'.join(lines)
