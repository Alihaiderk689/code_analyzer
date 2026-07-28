from django.test import SimpleTestCase

from ..services.patch_parser import parse_commentable_lines

# A single hunk starting at new-file line 10:
#   line 10 (context)  "def add(a, b):"
#   line 11 (removed)  "    return a+b"       <- not in new file, no line number
#   line 11 (added)    "    return a + b"
#   line 12 (added)    "    "
#   line 13 (context)  "def sub(a, b):"
_SINGLE_HUNK_PATCH = (
    '@@ -10,3 +10,4 @@\n'
    ' def add(a, b):\n'
    '-    return a+b\n'
    '+    return a + b\n'
    '+    \n'
    ' def sub(a, b):'
)


class ParseCommentableLinesTests(SimpleTestCase):
    def test_empty_patch_returns_empty_set(self):
        self.assertEqual(parse_commentable_lines(''), set())

    def test_none_like_patch_returns_empty_set(self):
        self.assertEqual(parse_commentable_lines(None), set())

    def test_single_hunk_maps_added_and_context_lines_to_new_file_numbers(self):
        # Removed line consumes no new-file number, so added lines land on
        # 11 and 12, and the trailing context line lands on 13.
        self.assertEqual(parse_commentable_lines(_SINGLE_HUNK_PATCH), {10, 11, 12, 13})

    def test_multiple_hunks_are_each_tracked_from_their_own_header(self):
        patch = (
            '@@ -1,2 +1,2 @@\n'
            ' unchanged line one\n'
            '+added line two\n'
            '@@ -50,2 +51,3 @@\n'
            ' unchanged line fifty-one\n'
            '+added line fifty-two\n'
            '+added line fifty-three\n'
        )
        self.assertEqual(parse_commentable_lines(patch), {1, 2, 51, 52, 53})

    def test_no_newline_at_end_of_file_marker_is_ignored_not_counted(self):
        patch = (
            '@@ -1,1 +1,1 @@\n'
            '+final line\n'
            '\\ No newline at end of file'
        )
        self.assertEqual(parse_commentable_lines(patch), {1})

    def test_pure_deletion_hunk_yields_no_commentable_lines(self):
        patch = '@@ -5,2 +5,0 @@\n-old line one\n-old line two'
        self.assertEqual(parse_commentable_lines(patch), set())

    def test_content_before_any_hunk_header_is_ignored_defensively(self):
        patch = 'diff --git a/file.py b/file.py\nindex abc..def 100644\n@@ -1,1 +1,1 @@\n+only line'
        self.assertEqual(parse_commentable_lines(patch), {1})
