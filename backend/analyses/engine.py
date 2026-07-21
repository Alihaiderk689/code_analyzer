import ast
import re

import parso
from pyflakes.checker import Checker

from . import sandbox

_PARSO_GRAMMAR = parso.load_grammar()

LANGUAGE_BY_EXTENSION = {
    '.py': 'Python',
    '.js': 'JavaScript',
    '.jsx': 'JavaScript',
    '.ts': 'TypeScript',
    '.tsx': 'TypeScript',
    '.java': 'Java',
    '.go': 'Go',
    '.rb': 'Ruby',
    '.php': 'PHP',
    '.cs': 'C#',
    '.cpp': 'C++',
    '.cc': 'C++',
    '.c': 'C',
    '.rs': 'Rust',
    '.kt': 'Kotlin',
    '.swift': 'Swift',
    '.html': 'HTML',
    '.css': 'CSS',
}

MAX_LINE_LENGTH = 120
TODO_PATTERN = re.compile(r'\b(TODO|FIXME|HACK|XXX)\b', re.IGNORECASE)
COMMENT_PREFIXES = ('#', '//', '/*', '*', '--')
ISSUE_PENALTIES = {
    'todo': 3,
    'long_line': 1,
    'no_comments': 5,
    'syntax_error': 40,
    'undefined_name': 15,
    'undefined_export': 10,
    'duplicate_argument': 10,
    'import_star_used': 5,
    'redefined_while_unused': 5,
    'unused_variable': 2,
    'unused_import': 2,
    'runtime_error': 50,
    'execution_timeout': 30,
}

# pyflakes message class name -> (issue type, penalty override or None to use the
# class-name-derived default above).
_PYFLAKES_TYPE_OVERRIDES = {
    'UndefinedName': 'undefined_name',
    'UndefinedExport': 'undefined_export',
    'DuplicateArgument': 'duplicate_argument',
    'ImportStarUsed': 'import_star_used',
    'RedefinedWhileUnused': 'redefined_while_unused',
    'UnusedVariable': 'unused_variable',
    'UnusedImport': 'unused_import',
}


def _camel_to_snake(name):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def _dedupe_cascading_syntax_errors(errors):
    """parso's error-recovery parser reports a spurious 'IndentationError: unexpected
    indent' on the line right after almost every real syntax error - a downstream
    artifact of the same root cause, not a separate mistake. Collapse those so each
    genuine mistake is reported once."""
    kept = []
    prev_line = None
    for err in errors:
        line = err.start_pos[0]
        if err.message == 'IndentationError: unexpected indent' and prev_line is not None and line == prev_line + 1:
            continue
        kept.append(err)
        prev_line = line
    return kept


def _python_issues(code):
    """Real syntax + undefined-name/unused-import checks for Python via ast + pyflakes.
    Other languages only get the generic textual checks below - there's no equivalent
    parser wired up for them here."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # ast.parse() stops at the first syntax error; parso does error recovery and
        # can keep going, so it can surface every syntax mistake in one pass instead
        # of just the first one.
        parso_tree = _PARSO_GRAMMAR.parse(code, error_recovery=True)
        errors = _dedupe_cascading_syntax_errors(list(_PARSO_GRAMMAR.iter_errors(parso_tree)))
        return [
            {
                'line': err.start_pos[0],
                'type': 'syntax_error',
                'message': (
                    f'{err.message}. Undefined-name/unused-import checks are skipped while any '
                    'syntax error exists in the file.'
                ),
            }
            for err in errors
        ]

    issues = []
    for message in Checker(tree).messages:
        issue_type = _PYFLAKES_TYPE_OVERRIDES.get(type(message).__name__, _camel_to_snake(type(message).__name__))
        issues.append({
            'line': message.lineno,
            'type': issue_type,
            'message': message.message % message.message_args,
        })

    # Only worth actually running the code once it's known to at least parse cleanly -
    # a file with a syntax error already returned above and never reaches this point.
    issues.extend(_python_runtime_issues(code))
    return issues


def _python_runtime_issues(code):
    """Sandboxed execution catches the *first* uncaught runtime error a script would
    hit - things like IndexError/KeyError/ZeroDivisionError that no static analyzer
    can predict without running the code. This can only ever surface one such error
    per analysis, the same way running the script yourself would stop at the first
    uncaught exception; see sandbox.py for exactly what is and isn't sandboxed."""
    result = sandbox.run_python(code)

    if result['status'] == 'error':
        message = result['exception_type']
        if result['message']:
            message += f": {result['message']}"
        return [{'line': result['line'], 'type': 'runtime_error', 'message': message}]

    if result['status'] == 'timeout':
        return [{
            'line': None,
            'type': 'execution_timeout',
            'message': f'Execution exceeded {sandbox.TIMEOUT_SECONDS} seconds and was terminated - possible infinite loop.',
        }]

    # 'ok' -> ran cleanly, nothing to report. 'unavailable' -> no sandboxing primitive
    # on this host (see sandbox.is_available) - degrades silently to static-only
    # analysis rather than surfacing a "sandbox unavailable" issue on every submission.
    return []


def detect_language(filename):
    lower = filename.lower()
    for ext, lang in LANGUAGE_BY_EXTENSION.items():
        if lower.endswith(ext):
            return lang
    return 'Unknown'


def analyze_code(code, language='Unknown'):
    lines = code.splitlines()
    lines_of_code = len([line for line in lines if line.strip()])

    issues = []
    comment_lines = 0
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if TODO_PATTERN.search(stripped):
            issues.append({'line': i, 'type': 'todo', 'message': f'Unresolved marker: {stripped[:80]}'})
        if len(line) > MAX_LINE_LENGTH:
            issues.append({
                'line': i, 'type': 'long_line',
                'message': f'Line exceeds {MAX_LINE_LENGTH} characters ({len(line)}).',
            })
        if stripped.startswith(COMMENT_PREFIXES):
            comment_lines += 1

    if lines_of_code >= 30 and comment_lines == 0:
        issues.append({'line': None, 'type': 'no_comments', 'message': 'File has no comments.'})

    if language == 'Python':
        issues.extend(_python_issues(code))

    quality_score = _score(lines_of_code, issues)
    return {
        'lines_of_code': lines_of_code,
        'issues': issues,
        'issues_count': len(issues),
        'quality_score': quality_score,
    }


def _score(lines_of_code, issues):
    if lines_of_code == 0:
        return 0.0
    penalty = sum(ISSUE_PENALTIES.get(issue['type'], 2) for issue in issues)
    return round(max(0.0, 100.0 - penalty), 1)
