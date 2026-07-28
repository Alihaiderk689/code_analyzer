"""Wraps the Bandit CLI (the standard Python static-analysis security linter)
as a BaseSecurityScanner.

Runs Bandit out-of-process against a temp file - the same subprocess
isolation analyses/sandbox.py already uses for runtime checks. That keeps a
third-party tool's own bugs/crashes/hangs from ever taking down the request,
and means upgrading Bandit is a requirements.txt bump, never a code change
here.

Bandit only understands Python; SecurityAnalysisService is responsible for
not invoking this scanner for other languages.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .types import BaseSecurityScanner, SecurityFinding, Severity, VulnerabilityType

logger = logging.getLogger(__name__)

BANDIT_TIMEOUT_SECONDS = 20

# Bandit itself only has LOW/MEDIUM/HIGH. These specific checks are escalated
# to CRITICAL here because they're conventionally treated that way (OWASP-
# style) regardless of the confidence Bandit happens to assign: arbitrary code
# execution, unauthenticated pickle, and raw/string-built SQL.
_CRITICAL_RULE_IDS = {
    'B608',  # hardcoded_sql_expressions
    'B610', 'B611',  # django_extra_used / django_rawsql_used
    'B301',  # pickle.loads
    'B307',  # eval
    'B602',  # subprocess with shell=True
    'B609',  # wildcard shell injection
}

_SEVERITY_MAP = {
    'LOW': Severity.LOW,
    'MEDIUM': Severity.MEDIUM,
    'HIGH': Severity.HIGH,
}

# Bandit test ID -> our vulnerability taxonomy. Deliberately not exhaustive:
# any test ID not listed here still comes through as OTHER rather than being
# dropped, so a gap in this table can never make us silently lose a real
# finding - it just shows up less specifically categorized.
_RULE_TYPE_MAP: dict[str, VulnerabilityType] = {
    'B608': VulnerabilityType.SQL_INJECTION,
    'B610': VulnerabilityType.SQL_INJECTION,
    'B611': VulnerabilityType.SQL_INJECTION,
    'B105': VulnerabilityType.HARDCODED_PASSWORD,
    'B106': VulnerabilityType.HARDCODED_PASSWORD,
    'B107': VulnerabilityType.HARDCODED_PASSWORD,
    'B103': VulnerabilityType.SENSITIVE_DATA_EXPOSURE,
    'B104': VulnerabilityType.SENSITIVE_DATA_EXPOSURE,
    'B108': VulnerabilityType.SENSITIVE_DATA_EXPOSURE,
    'B301': VulnerabilityType.UNSAFE_DESERIALIZATION,
    'B302': VulnerabilityType.UNSAFE_DESERIALIZATION,
    'B403': VulnerabilityType.UNSAFE_DESERIALIZATION,
    'B506': VulnerabilityType.UNSAFE_DESERIALIZATION,  # yaml.load
    'B303': VulnerabilityType.WEAK_PASSWORD_STORAGE,
    'B304': VulnerabilityType.WEAK_PASSWORD_STORAGE,
    'B324': VulnerabilityType.WEAK_PASSWORD_STORAGE,
    'B311': VulnerabilityType.WEAK_RANDOMNESS,
    'B307': VulnerabilityType.COMMAND_INJECTION,
    'B601': VulnerabilityType.COMMAND_INJECTION,
    'B602': VulnerabilityType.COMMAND_INJECTION,
    'B603': VulnerabilityType.COMMAND_INJECTION,
    'B604': VulnerabilityType.COMMAND_INJECTION,
    'B605': VulnerabilityType.COMMAND_INJECTION,
    'B606': VulnerabilityType.COMMAND_INJECTION,
    'B607': VulnerabilityType.COMMAND_INJECTION,
    'B609': VulnerabilityType.COMMAND_INJECTION,
    'B308': VulnerabilityType.XSS,  # mark_safe
    'B701': VulnerabilityType.XSS,  # jinja2_autoescape_false
    'B703': VulnerabilityType.XSS,  # django_mark_safe
    'B201': VulnerabilityType.DEBUG_ENABLED,  # flask_debug_true
}


class BanditScanner(BaseSecurityScanner):
    name = 'bandit'

    def scan(self, source_code: str, filename: str = 'submission.py', settings_source: str = '') -> list[SecurityFinding]:
        with tempfile.TemporaryDirectory() as scratch_dir:
            script_path = Path(scratch_dir) / Path(filename).name
            script_path.write_text(source_code)

            try:
                result = subprocess.run(
                    ['bandit', '-f', 'json', str(script_path)],
                    capture_output=True,
                    timeout=BANDIT_TIMEOUT_SECONDS,
                    text=True,
                )
            except FileNotFoundError:
                logger.error('Bandit is not installed or not on PATH - security scan skipped.')
                return []
            except subprocess.TimeoutExpired:
                logger.warning('Bandit scan timed out after %ss - security scan skipped.', BANDIT_TIMEOUT_SECONDS)
                return []

            try:
                payload = json.loads(result.stdout or '{}')
            except json.JSONDecodeError:
                logger.error('Could not parse Bandit output as JSON. stderr: %s', (result.stderr or '')[:500])
                return []

            for err in payload.get('errors', []):
                # e.g. a syntax error in the submitted code - not a scanner bug,
                # just means Bandit couldn't build an AST to check. Not fatal:
                # other scanners (custom_rules) still run.
                logger.info('Bandit could not fully analyze the input: %s', err)

            return [self._to_finding(item, source_code) for item in payload.get('results', [])]

    def _to_finding(self, item: dict, source_code: str) -> SecurityFinding:
        rule_id = item.get('test_id', 'UNKNOWN')
        severity = _SEVERITY_MAP.get(item.get('issue_severity', 'MEDIUM'), Severity.MEDIUM)
        if rule_id in _CRITICAL_RULE_IDS:
            severity = Severity.CRITICAL
        line_number = item.get('line_number')

        return SecurityFinding(
            scanner=self.name,
            rule_id=rule_id,
            vulnerability_type=_RULE_TYPE_MAP.get(rule_id, VulnerabilityType.OTHER),
            severity=severity,
            description=item.get('issue_text', ''),
            line_number=line_number,
            code_snippet=item.get('code') or self._snippet(source_code, line_number),
            confidence=item.get('issue_confidence'),
        )

    @staticmethod
    def _snippet(source_code: str, line_number: Optional[int], context: int = 1) -> str:
        """Fallback only - Bandit's own `code` field (used above) already
        includes a numbered snippet in virtually all cases."""
        if not line_number:
            return ''
        lines = source_code.splitlines()
        start = max(0, line_number - 1 - context)
        end = min(len(lines), line_number + context)
        return '\n'.join(lines[start:end])
