"""Lightweight pattern-based performance-smell detection for PR file analysis.

Deliberately simple, disclosed line/regex heuristics - the same style and
spirit as analyses.services.custom_rules_service's security rules - not a
profiler or a full static analyzer. Each rule is a well-known, low-false-
positive smell that's genuinely detectable from a single line of source
without needing an AST or cross-line context.
"""
from __future__ import annotations

import re


class PerformanceIssueType:
    RANGE_LEN = 'range_len'
    REQUESTS_NO_TIMEOUT = 'requests_no_timeout'
    SELECT_STAR = 'select_star'
    BLOCKING_SLEEP = 'blocking_sleep'


# (type, pattern, severity, message, explanation, remediation)
_LINE_RULES: list[tuple[str, re.Pattern, str, str, str, str]] = [
    (
        PerformanceIssueType.RANGE_LEN,
        re.compile(r'\brange\(len\('),
        'medium',
        'Inefficient loop: range(len(...))',
        'Iterating with range(len(...)) is slower and less readable than iterating directly over '
        'the sequence, and requires an extra indexing operation on every loop iteration.',
        'Iterate directly over the sequence, or use enumerate(sequence) if you also need the index.',
    ),
    (
        PerformanceIssueType.REQUESTS_NO_TIMEOUT,
        re.compile(r'\brequests\.(get|post|put|patch|delete)\('),
        'medium',
        'HTTP request without a timeout',
        'A request with no timeout can hang indefinitely if the remote server stalls or the network '
        'stops responding, blocking whatever thread/process made the call.',
        'Pass an explicit timeout to every requests call, e.g. timeout=10.',
    ),
    (
        PerformanceIssueType.SELECT_STAR,
        re.compile(r'SELECT\s+\*\s+FROM', re.IGNORECASE),
        'low',
        'SQL SELECT * fetches unnecessary columns',
        'SELECT * fetches every column even when only a few are needed, wasting bandwidth and memory '
        'as the table grows.',
        'List only the columns the query actually uses.',
    ),
    (
        PerformanceIssueType.BLOCKING_SLEEP,
        re.compile(r'\btime\.sleep\('),
        'low',
        'Blocking sleep call',
        'A blocking sleep pauses the entire thread/process rather than yielding control, which can '
        'stall request handling if this code runs on a request path.',
        'Use a non-blocking/async delay mechanism, or move this off the request-handling path.',
    ),
]


_TIMEOUT_CONTEXT_LINES = 3


def find_performance_issues(source_code: str) -> list[dict]:
    """Returns issues shaped like pr_analysis_service's other sources:
    {source: 'performance', type, severity, line, message, explanation, remediation}."""
    issues: list[dict] = []
    lines = source_code.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for issue_type, pattern, severity, message, explanation, remediation in _LINE_RULES:
            if not pattern.search(line):
                continue
            if issue_type == PerformanceIssueType.REQUESTS_NO_TIMEOUT:
                # A multi-line call (`requests.get(\n    url,\n    timeout=10,\n)`)
                # is common Python style and puts `timeout=` on a different
                # line than the matched `requests.get(` - checking only the
                # exact line flagged every well-formed multi-line call as if
                # it had no timeout at all. A few lines of context either
                # side covers that (and a timeout applied via a nearby
                # session/adapter setup) without needing a real parser.
                context = lines[max(0, lineno - 1 - _TIMEOUT_CONTEXT_LINES):lineno + _TIMEOUT_CONTEXT_LINES]
                if any('timeout' in ctx_line for ctx_line in context):
                    continue
            issues.append({
                'source': 'performance',
                'type': issue_type,
                'severity': severity,
                'line': lineno,
                'message': message,
                'explanation': explanation,
                'remediation': remediation,
            })
    return issues
