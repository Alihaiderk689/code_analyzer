"""Lightweight pattern-based checks for security issues Bandit structurally
can't see - framework-level configuration (DEBUG=True, CSRF disabled) and
cross-cutting concerns (missing auth, unsafe file uploads, path traversal, a
broader hardcoded-secret pattern than Bandit's narrow password-string check).

Bandit is an AST-based *Python* tool built around "dangerous function call"
patterns; it has no concept of "is this Django view protected?" or "is DEBUG
on?". Without this scanner, several categories this project is required to
detect (missing authentication/authorization, CSRF disabled, DEBUG=True,
unsafe file uploads, path traversal) would simply never fire. This is
deliberately simple line/regex-based matching, the same style as
analyses/engine.py's TODO/long-line checks - these are heuristics, not a full
analyzer, and that's disclosed here rather than pretending otherwise.
"""
from __future__ import annotations

import re

from .types import BaseSecurityScanner, SecurityFinding, Severity, VulnerabilityType

# (rule_id, pattern, vulnerability_type, severity, description) - each checked
# independently against every line. Order doesn't matter; a line can match
# more than one rule.
_LINE_RULES: list[tuple[str, re.Pattern, VulnerabilityType, Severity, str]] = [
    (
        'CUSTOM_DEBUG_TRUE',
        re.compile(r'^\s*DEBUG\s*=\s*True\b'),
        VulnerabilityType.DEBUG_ENABLED,
        Severity.HIGH,
        'Debug mode is enabled, which can leak stack traces, settings, and other '
        'internals to end users in production.',
    ),
    (
        'CUSTOM_CSRF_EXEMPT',
        re.compile(r'@csrf_exempt|csrf_protect\s*=\s*False|CSRF_COOKIE_SECURE\s*=\s*False'),
        VulnerabilityType.CSRF_DISABLED,
        Severity.HIGH,
        'CSRF protection appears to be explicitly disabled for this view/setting.',
    ),
    (
        'CUSTOM_HARDCODED_SECRET',
        re.compile(
            r'\b(SECRET_KEY|API_KEY|ACCESS_KEY|SECRET_ACCESS_KEY|PRIVATE_KEY|AUTH_TOKEN|'
            r'CLIENT_SECRET)\s*=\s*[\'"][^\'"]{8,}[\'"]',
            re.IGNORECASE,
        ),
        VulnerabilityType.HARDCODED_SECRET,
        Severity.CRITICAL,
        'A secret/API key/token appears to be hardcoded directly in source rather than '
        'loaded from environment/config.',
    ),
    (
        'CUSTOM_UNSAFE_FILE_UPLOAD',
        re.compile(r'request\.FILES\b'),
        VulnerabilityType.UNSAFE_FILE_UPLOAD,
        Severity.MEDIUM,
        'File upload handling found - verify file type, size, and content are validated '
        'before the file is trusted or stored.',
    ),
]

_AUTH_DECORATOR_PATTERN = re.compile(
    r'@(login_required|permission_required|permission_classes|api_view|staff_member_required)\b'
)
# Deliberately top-level only (no leading whitespace, no `self` param): an
# indented `def` is a method of some class, which the class-level check below
# already judges by looking at the whole class body. Matching methods here
# too used to double-count the same view under both checks - sometimes with
# a wrong-reason "missing_authentication" hit from this check that papered
# over the class-level check's real blind spot (see _scan_missing_auth).
_VIEW_DEF_PATTERN = re.compile(r'^def\s+\w+\s*\(\s*request\b')
_CLASS_VIEW_PATTERN = re.compile(r'^\s*class\s+(?P<name>\w+)\s*\((?P<bases>[^)]*)\)\s*:')
_CLASS_BODY_LOOKAHEAD_LINES = 15
_PERMISSION_CLASSES_VALUE_PATTERN = re.compile(r'permission_classes\s*=\s*[\[(](?P<body>[^\])]*)[\])]')
_ALLOW_ANY_PATTERN = re.compile(r'\bAllowAny\b')
# DRF's DEFAULT_PERMISSION_CLASSES lives in a settings file the analyzed view
# file itself never mentions - matched against whatever settings source the
# caller could find (see pr_analysis_service._find_settings_source). Handles
# both the dict-key form ('DEFAULT_PERMISSION_CLASSES': [...]) and a bare
# module-level assignment; deliberately simple bracket matching (no nested
# brackets expected in a class-path list), same style as the rest of this file.
_DEFAULT_PERMISSION_BLOCK_PATTERN = re.compile(
    r'DEFAULT_PERMISSION_CLASSES[\'"]?\s*[:=]\s*[\[(](?P<body>[^\])]*)[\])]', re.DOTALL,
)


def _has_restrictive_permission_default(settings_source: str) -> bool:
    """True only when a DEFAULT_PERMISSION_CLASSES block was found and it
    doesn't include AllowAny - i.e. the project is known to reject requests
    by default, so a view with no explicit permission_classes of its own is
    actually covered rather than genuinely open."""
    if not settings_source:
        return False
    match = _DEFAULT_PERMISSION_BLOCK_PATTERN.search(settings_source)
    if not match:
        return False
    body = match.group('body').strip()
    return bool(body) and not _ALLOW_ANY_PATTERN.search(body)


# Path traversal needs a little more than one line of context: the common,
# realistic shape is "path = request.GET[...]" followed a few lines later by
# "open(path)" - not always the single-line "open(request.GET[...])" case. This
# is lightweight variable-taint tracking (does *this* open() use a name that
# was recently assigned from user input), not full data-flow analysis.
_TAINT_SOURCE_PATTERN = re.compile(r'^\s*(\w+)\s*=.*\b(request\.(GET|POST|FILES)|input\(|sys\.argv)')
# A handful of common sanitizers - if the closest preceding assignment to the
# same variable runs it through one of these, treat the taint as cleared
# rather than flagging code that already defends against traversal.
_SANITIZER_PATTERN = re.compile(r'^\s*(\w+)\s*=.*\b(os\.path\.basename|secure_filename)\(')
_OPEN_CALL_PATTERN = re.compile(r'\bopen\(\s*(\w+)\b')
_TAINT_LOOKBACK_LINES = 10


class CustomRulesScanner(BaseSecurityScanner):
    name = 'custom_rules'

    def scan(self, source_code: str, filename: str = 'submission.py', settings_source: str = '') -> list[SecurityFinding]:
        lines = source_code.splitlines()
        findings = self._scan_line_rules(lines)
        findings.extend(self._scan_missing_auth(lines, _has_restrictive_permission_default(settings_source)))
        findings.extend(self._scan_path_traversal(lines))
        return findings

    def _scan_line_rules(self, lines: list[str]) -> list[SecurityFinding]:
        findings = []
        for i, line in enumerate(lines, start=1):
            for rule_id, pattern, vuln_type, severity, description in _LINE_RULES:
                if pattern.search(line):
                    findings.append(SecurityFinding(
                        scanner=self.name,
                        rule_id=rule_id,
                        vulnerability_type=vuln_type,
                        severity=severity,
                        description=description,
                        line_number=i,
                        code_snippet=line.strip(),
                    ))
        return findings

    def _scan_missing_auth(self, lines: list[str], has_restrictive_default: bool = False) -> list[SecurityFinding]:
        """Heuristic, intentionally lower-severity than the pattern rules above:
        this can only ever say "no auth decorator visible nearby", never "this
        is definitely unprotected" (auth might be enforced by middleware, a
        base class elsewhere, etc.) - it's a prompt to double-check, not a
        confident assertion."""
        findings = []
        for i, line in enumerate(lines, start=1):
            if _VIEW_DEF_PATTERN.match(line):
                preceding = '\n'.join(lines[max(0, i - 4):i - 1])
                if not _AUTH_DECORATOR_PATTERN.search(preceding):
                    findings.append(SecurityFinding(
                        scanner=self.name,
                        rule_id='CUSTOM_MISSING_AUTHENTICATION',
                        vulnerability_type=VulnerabilityType.MISSING_AUTHENTICATION,
                        severity=Severity.LOW,
                        description=(
                            'No @login_required/@permission_classes/@api_view decorator was found '
                            'directly above this view function - verify authentication is enforced '
                            'elsewhere (middleware, a shared base) if this is intentional.'
                        ),
                        line_number=i,
                        code_snippet=line.strip(),
                    ))
                continue

            class_match = _CLASS_VIEW_PATTERN.match(line)
            if class_match and 'View' in class_match.group('bases'):
                if 'LoginRequiredMixin' in class_match.group('bases'):
                    continue
                body = '\n'.join(lines[i:i + _CLASS_BODY_LOOKAHEAD_LINES])
                perm_match = _PERMISSION_CLASSES_VALUE_PATTERN.search(body)

                if perm_match is None:
                    # No explicit declaration - only worth flagging when we don't
                    # already know the project rejects requests by default (see
                    # _has_restrictive_permission_default); otherwise this view
                    # really is covered, just not by anything visible in this file.
                    if has_restrictive_default:
                        continue
                    findings.append(SecurityFinding(
                        scanner=self.name,
                        rule_id='CUSTOM_MISSING_AUTHORIZATION',
                        vulnerability_type=VulnerabilityType.MISSING_AUTHORIZATION,
                        severity=Severity.LOW,
                        description=(
                            f'View class "{class_match.group("name")}" has no explicit permission_classes - '
                            'it falls back to whatever default permissions the project has configured '
                            '(or none, if unset). Declare it explicitly if this endpoint needs restricting.'
                        ),
                        line_number=i,
                        code_snippet=line.strip(),
                    ))
                elif _ALLOW_ANY_PATTERN.search(perm_match.group('body')):
                    # Explicit is better than missing, but AllowAny on a class
                    # that otherwise looks like it needs restricting (e.g. an
                    # admin/internal-sounding name) is exactly the kind of typo/
                    # copy-paste mistake worth a second look - previously this
                    # was invisible because the old check only looked for the
                    # substring "permission_classes", never its actual value.
                    findings.append(SecurityFinding(
                        scanner=self.name,
                        rule_id='CUSTOM_PERMISSIVE_AUTHORIZATION',
                        vulnerability_type=VulnerabilityType.MISSING_AUTHORIZATION,
                        severity=Severity.LOW,
                        description=(
                            f'View class "{class_match.group("name")}" explicitly sets '
                            'permission_classes = [AllowAny] - verify this endpoint is really meant '
                            'to be open to unauthenticated users.'
                        ),
                        line_number=i,
                        code_snippet=line.strip(),
                    ))
        return findings

    def _scan_path_traversal(self, lines: list[str]) -> list[SecurityFinding]:
        findings = []
        for i, line in enumerate(lines, start=1):
            open_match = _OPEN_CALL_PATTERN.search(line)
            if not open_match:
                continue

            arg_name = open_match.group(1)
            # Inline case - open(request.GET[...]) directly - has no variable
            # name to check, but the call site itself already contains the
            # taint source text.
            if re.search(r'request\.|input\(|sys\.argv', line):
                tainted = True
            else:
                lookback = lines[max(0, i - 1 - _TAINT_LOOKBACK_LINES):i - 1]
                # Walk backwards so the MOST RECENT assignment to arg_name wins -
                # a sanitizer call reassigning the same name after the taint
                # source clears it, matching how the variable actually behaves
                # by the time open() runs. Checking "was it EVER assigned from
                # a taint source" (old behaviour) ignored any sanitization that
                # happened afterward.
                tainted = False
                for prev_line in reversed(lookback):
                    sanitizer_match = _SANITIZER_PATTERN.match(prev_line)
                    if sanitizer_match and sanitizer_match.group(1) == arg_name:
                        break
                    taint_match = _TAINT_SOURCE_PATTERN.match(prev_line)
                    if taint_match and taint_match.group(1) == arg_name:
                        tainted = True
                        break

            if tainted:
                findings.append(SecurityFinding(
                    scanner=self.name,
                    rule_id='CUSTOM_PATH_TRAVERSAL',
                    vulnerability_type=VulnerabilityType.PATH_TRAVERSAL,
                    severity=Severity.HIGH,
                    description=(
                        'A file path built from user-controlled input is passed to open() with no '
                        'visible sanitization - a "../" style payload could escape the intended directory.'
                    ),
                    line_number=i,
                    code_snippet=line.strip(),
                ))
        return findings
