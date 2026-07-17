import re

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
ISSUE_PENALTIES = {'todo': 3, 'long_line': 1, 'no_comments': 5}


def detect_language(filename):
    lower = filename.lower()
    for ext, lang in LANGUAGE_BY_EXTENSION.items():
        if lower.endswith(ext):
            return lang
    return 'Unknown'


def analyze_code(code):
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
