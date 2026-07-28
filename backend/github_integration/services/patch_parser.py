"""Parses a unified diff ('patch', as returned by GitHub's PR-files API) to
find which new-file line numbers are valid targets for an inline review
comment. GitHub's review API rejects an inline comment on a line that isn't
part of the diff, so this is what lets comment_service decide "attach to this
line" vs "fall back to the summary" per the spec.
"""
from __future__ import annotations

import re

_HUNK_HEADER_PATTERN = re.compile(r'^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@')


def parse_commentable_lines(patch: str) -> set[int]:
    """Returns the set of new-file (RIGHT side) line numbers this diff touches
    or shows as context - both count as commentable via GitHub's API, only
    lines entirely outside every hunk don't."""
    if not patch:
        return set()

    commentable: set[int] = set()
    new_line = None

    for line in patch.splitlines():
        header_match = _HUNK_HEADER_PATTERN.match(line)
        if header_match:
            new_line = int(header_match.group('new_start'))
            continue
        if new_line is None:
            continue  # content before any hunk header - shouldn't happen, ignore defensively

        if line.startswith('+'):
            commentable.add(new_line)
            new_line += 1
        elif line.startswith('-'):
            pass  # old-file-only line - doesn't exist in the new file at all
        elif line.startswith('\\'):
            pass  # "\ No newline at end of file" marker - not a content line
        else:
            # unchanged context line - exists in both old and new, at this new-file line
            commentable.add(new_line)
            new_line += 1

    return commentable
