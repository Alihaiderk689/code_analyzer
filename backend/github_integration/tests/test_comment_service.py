from unittest.mock import patch

from django.test import TestCase

from ..services.comment_service import MAX_INLINE_COMMENTS, CommentService
from ..services.github_client import GitHubAPIError
from .factories import make_file_analysis, make_integration, make_pr_analysis, make_repository, make_user

_PATCH = '@@ -1,3 +1,3 @@\n def line_one():\n-    old\n+    new\n def line_three():'
# commentable new-file lines from this patch: 1, 2, 3


def _issue(**overrides):
    defaults = dict(
        source='security', type='sql_injection', severity='high', line=2,
        message='Possible SQL injection.', explanation='User input reaches the query unsanitized.',
        remediation='Use parameterized queries instead.',
    )
    defaults.update(overrides)
    return defaults


class BuildInlineCommentsTests(TestCase):
    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))
        self.pr_analysis = make_pr_analysis(self.repository)

    def test_issue_on_a_commentable_line_becomes_inline_comment(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[_issue(line=2)])
        inline, overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        self.assertEqual(len(inline), 1)
        self.assertEqual(overflow, [])
        self.assertEqual(inline[0]['path'], file_analysis.file_path)
        self.assertEqual(inline[0]['line'], 2)
        self.assertEqual(inline[0]['side'], 'RIGHT')

    def test_issue_on_a_line_outside_the_diff_falls_back_to_overflow(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[_issue(line=999)])
        inline, overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        self.assertEqual(inline, [])
        self.assertEqual(len(overflow), 1)

    def test_issue_with_no_line_falls_back_to_overflow(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[_issue(line=None)])
        inline, overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        self.assertEqual(inline, [])
        self.assertEqual(len(overflow), 1)

    def test_excess_past_max_inline_comments_overflows(self):
        issues = [_issue(line=2, type=f'issue_{i}') for i in range(MAX_INLINE_COMMENTS + 5)]
        file_analysis = make_file_analysis(self.pr_analysis, issues=issues)
        inline, overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        self.assertEqual(len(inline), MAX_INLINE_COMMENTS)
        self.assertEqual(len(overflow), 5)

    def test_comment_body_includes_severity_message_and_remediation(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[_issue(line=2)])
        inline, _overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        body = inline[0]['body']
        self.assertIn('High Severity', body)
        self.assertIn('Possible SQL injection.', body)
        self.assertIn('Use parameterized queries instead.', body)

    def test_ai_explanation_containing_a_mention_is_defanged(self):
        # Simulates a prompt-injection attempt succeeding against the AI
        # enrichment step (analyses.services.ai_security_service) and the
        # model returning text containing a live-looking @mention - this must
        # not become a real GitHub notification when posted.
        file_analysis = make_file_analysis(self.pr_analysis, issues=[
            _issue(line=2, explanation='cc @someone-unrelated please review this urgently'),
        ])
        inline, _overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        body = inline[0]['body']
        self.assertNotIn('@someone-unrelated', body)
        self.assertIn('someone-unrelated', body)  # text preserved, just defanged

    def test_ai_remediation_containing_raw_html_is_stripped(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[
            _issue(line=2, remediation='<img src=x onerror=alert(1)>Use a safer approach.'),
        ])
        inline, _overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        body = inline[0]['body']
        self.assertNotIn('<img', body)
        self.assertNotIn('onerror', body)
        self.assertIn('Use a safer approach.', body)

    def test_ai_text_containing_a_url_is_defanged_not_auto_linked(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[
            _issue(line=2, explanation='See https://evil.example.com/phish for details.'),
        ])
        inline, _overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        body = inline[0]['body']
        self.assertIn('`https://evil.example.com/phish`', body)

    def test_oversized_ai_text_is_truncated(self):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[
            _issue(line=2, explanation='A' * 5000),
        ])
        inline, _overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        body = inline[0]['body']
        # Well under the raw 5000 chars - proves truncation actually happened,
        # without hardcoding the exact cap here.
        self.assertLess(len(body), 2000)

    def test_non_string_explanation_and_remediation_are_dropped_not_posted_raw(self):
        # A malformed/adversarial AI response could in principle leave a
        # non-string value on these fields (see ai_security_service's own
        # type-checking) - CommentService must not choke on or blindly
        # stringify one either, as a second, independent layer.
        file_analysis = make_file_analysis(self.pr_analysis, issues=[
            _issue(line=2, explanation={'not': 'a string'}, remediation=['also', 'not', 'a', 'string']),
        ])
        inline, _overflow = CommentService._build_inline_comments([(file_analysis, _PATCH)])
        body = inline[0]['body']
        self.assertNotIn('Suggested fix', body)  # remediation section omitted, not garbled


class BuildSummaryBodyTests(TestCase):
    def setUp(self):
        self.repository = make_repository(make_integration(make_user()))

    def test_includes_overall_score_and_summary_text(self):
        pr_analysis = make_pr_analysis(self.repository, overall_score=82.5, summary='Analyzed 3 file(s), found 1 issue(s).')
        body = CommentService._build_summary_body(pr_analysis, overflow=[])
        self.assertIn('82.5/100', body)
        self.assertIn('Analyzed 3 file(s)', body)

    def test_malicious_summary_is_sanitized(self):
        # summary is currently always built from numeric counts (see
        # pr_analysis_service._build_summary), but nothing enforces that at
        # this layer - it must be sanitized like every other free-text field
        # reaching a GitHub review body, not exempted because it happens to
        # be safe today.
        pr_analysis = make_pr_analysis(
            self.repository,
            summary='cc @someone <b>bold</b> see https://evil.example.com/phish for details',
        )
        body = CommentService._build_summary_body(pr_analysis, overflow=[])
        self.assertNotIn('@someone', body)
        self.assertNotIn('<b>', body)
        self.assertIn('`https://evil.example.com/phish`', body)
        self.assertIn('someone', body)  # text preserved, just defanged

    def test_overflow_issues_are_listed_with_file_and_line(self):
        pr_analysis = make_pr_analysis(self.repository)
        body = CommentService._build_summary_body(pr_analysis, overflow=[('app.py', _issue(line=42))])
        self.assertIn('app.py:42', body)

    def test_overflow_issue_without_line_shows_just_the_file(self):
        pr_analysis = make_pr_analysis(self.repository)
        body = CommentService._build_summary_body(pr_analysis, overflow=[('app.py', _issue(line=None))])
        self.assertIn('`app.py`', body)
        self.assertNotIn('app.py:None', body)

    def test_overflow_message_is_sanitized(self):
        pr_analysis = make_pr_analysis(self.repository)
        body = CommentService._build_summary_body(
            pr_analysis, overflow=[('app.py', _issue(line=1, message='cc @someone <b>bold</b> attempt'))],
        )
        self.assertNotIn('@someone', body)
        self.assertNotIn('<b>', body)


class PostReviewTests(TestCase):
    def setUp(self):
        self.repository = make_repository(make_integration(make_user()), full_name='octocat/hello-world')
        self.pr_analysis = make_pr_analysis(self.repository)

    @patch('github_integration.services.comment_service.GitHubClient')
    def test_posts_review_and_marks_review_posted(self, mock_client_cls):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[_issue(line=2)])

        CommentService().post_review(self.pr_analysis, [(file_analysis, _PATCH)], 'access-token')

        mock_client_cls.return_value.create_review.assert_called_once()
        args, kwargs = mock_client_cls.return_value.create_review.call_args
        self.assertEqual(args[:3], ('octocat', 'hello-world', self.pr_analysis.pull_request_number))
        self.pr_analysis.refresh_from_db()
        self.assertTrue(self.pr_analysis.review_posted)

    @patch('github_integration.services.comment_service.GitHubClient')
    def test_falls_back_to_summary_only_review_when_inline_comments_are_rejected(self, mock_client_cls):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[_issue(line=2)])
        mock_client_cls.return_value.create_review.side_effect = [GitHubAPIError('invalid comment'), {'id': 1}]

        CommentService().post_review(self.pr_analysis, [(file_analysis, _PATCH)], 'access-token')

        self.assertEqual(mock_client_cls.return_value.create_review.call_count, 2)
        second_call_kwargs = mock_client_cls.return_value.create_review.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs['comments'], [])
        self.pr_analysis.refresh_from_db()
        self.assertTrue(self.pr_analysis.review_posted)

    @patch('github_integration.services.comment_service.GitHubClient')
    def test_raises_when_there_were_no_inline_comments_to_fall_back_from(self, mock_client_cls):
        file_analysis = make_file_analysis(self.pr_analysis, issues=[])
        mock_client_cls.return_value.create_review.side_effect = GitHubAPIError('down')

        with self.assertRaises(GitHubAPIError):
            CommentService().post_review(self.pr_analysis, [(file_analysis, _PATCH)], 'access-token')

        self.pr_analysis.refresh_from_db()
        self.assertFalse(self.pr_analysis.review_posted)
