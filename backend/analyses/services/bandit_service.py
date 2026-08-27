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
import sys
import tempfile
from pathlib import Path
from typing import Optional

from core.execution_budget import STAGE_BANDIT

from .types import BaseSecurityScanner, ScannerUnavailable, SecurityFinding, Severity, VulnerabilityType

logger = logging.getLogger(__name__)

BANDIT_TIMEOUT_SECONDS = 20

# A Bandit run given less than this is not worth starting: it would almost
# certainly be killed and reported as `reason='timeout'`, which blames the
# scanner for our own deadline. Below this the scan is skipped outright and
# reported as REASON_BUDGET_EXHAUSTED instead.
MIN_BANDIT_SLICE_SECONDS = 5

# ScannerUnavailable.reason for "the request-wide budget stopped this", kept
# distinct from 'timeout' (Bandit really did hang), 'not_installed' and
# 'unparsable_output' - a scanner failure and a deadline need different fixes.
REASON_BUDGET_EXHAUSTED = 'budget_exhausted'

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

    def __init__(self, budget=None) -> None:
        self._unavailable: Optional[ScannerUnavailable] = None
        # Optional request-wide deadline (core/execution_budget.py), injected
        # via SecurityAnalysisService(scanners=[...]) by the repository-context
        # path only. None - every other caller - means the historical
        # behavior: a flat BANDIT_TIMEOUT_SECONDS with no total bound. Kept on
        # the constructor rather than scan() so BaseSecurityScanner.scan's
        # signature, which every scanner and test relies on, is untouched.
        self._budget = budget

    def consume_unavailable(self) -> Optional[ScannerUnavailable]:
        unavailable, self._unavailable = self._unavailable, None
        return unavailable

    def _mark_unavailable(self, reason: str, detail: str = '') -> list[SecurityFinding]:
        self._unavailable = ScannerUnavailable(scanner=self.name, reason=reason, detail=detail)
        return []

    def scan(self, source_code: str, filename: str = 'submission.py', settings_source: str = '') -> list[SecurityFinding]:
        self._unavailable = None
        with tempfile.TemporaryDirectory() as scratch_dir:
            script_path = Path(scratch_dir) / Path(filename).name
            script_path.write_text(source_code)

            timeout = BANDIT_TIMEOUT_SECONDS
            if self._budget is not None:
                if not self._budget.can_afford(MIN_BANDIT_SLICE_SECONDS, STAGE_BANDIT):
                    logger.warning('Bandit scan skipped - request budget exhausted.')
                    return self._mark_unavailable(
                        REASON_BUDGET_EXHAUSTED,
                        'Skipped: the request time budget was exhausted before Bandit could run.',
                    )
                timeout = min(BANDIT_TIMEOUT_SECONDS, self._budget.remaining())

            try:
                result = subprocess.run(
                    # `sys.executable -m bandit`, not a bare 'bandit': the
                    # console script lives in the interpreter's own bin/ dir,
                    # which is NOT on PATH when Python is invoked by absolute
                    # path (a venv that was never "activated", and some process
                    # managers). That made subprocess raise FileNotFoundError
                    # and the scan return no findings at all - silently, until
                    # scan_complete started reporting it. Going through the
                    # running interpreter binds the scanner to the same
                    # environment Django itself is running in.
                    [sys.executable, '-m', 'bandit', '-f', 'json', str(script_path)],
                    capture_output=True,
                    timeout=timeout,
                    text=True,
                )
            except FileNotFoundError:
                logger.error('Bandit is not installed or not on PATH - security scan skipped.')
                return self._mark_unavailable('not_installed', 'Bandit is not installed or not on PATH.')
            except subprocess.TimeoutExpired:
                if self._budget is not None and self._budget.expired(STAGE_BANDIT):
                    # Killed on a budget-clamped slice, not on Bandit's own
                    # 20s ceiling - reporting 'timeout' here would blame the
                    # scanner for the request running out of time.
                    self._budget.mark_skipped(STAGE_BANDIT)
                    logger.warning('Bandit scan cut short - request budget exhausted.')
                    return self._mark_unavailable(
                        REASON_BUDGET_EXHAUSTED,
                        'Cut short: the request time budget was exhausted while Bandit was running.',
                    )
                logger.warning('Bandit scan timed out after %ss - security scan skipped.', timeout)
                return self._mark_unavailable(
                    'timeout', f'Bandit did not finish within {timeout:g}s.',
                )

            try:
                payload = json.loads(result.stdout or '{}')
            except json.JSONDecodeError:
                logger.error('Could not parse Bandit output as JSON. stderr: %s', (result.stderr or '')[:500])
                return self._mark_unavailable('unparsable_output', 'Bandit output was not valid JSON.')

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
