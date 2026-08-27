"""Orchestrates the full Security Analysis Mode pipeline for an Analysis:

    run every registered scanner -> deduplicate -> enrich with AI-written
    explanation/remediation -> build the final scored report.

This is the only class views should import from analyses/services - it
composes the others so adding a new scanner (Semgrep is the planned one)
never means touching the view layer, only appending to `self.scanners` here.
"""
from __future__ import annotations

import logging

from .ai_security_service import AISecurityService
from .bandit_service import BanditScanner
from .custom_rules_service import CustomRulesScanner
from .report_generator import SecurityReportGenerator
from .types import BaseSecurityScanner, ScannerUnavailable, SecurityFinding

logger = logging.getLogger(__name__)

# Scanners that only make sense for a specific language. Bandit is an AST
# tool for Python; it can't run against JS/Java/etc. source at all. Scanners
# not listed here (e.g. CustomRulesScanner, and future Semgrep once it has
# multi-language rules) run regardless of detected language.
_LANGUAGE_RESTRICTED_SCANNERS: dict[str, str] = {
    'bandit': 'Python',
}


class SecurityAnalysisService:
    def __init__(
        self,
        scanners: list[BaseSecurityScanner] | None = None,
        ai_service: AISecurityService | None = None,
        report_generator: SecurityReportGenerator | None = None,
    ):
        self.scanners = scanners if scanners is not None else [BanditScanner(), CustomRulesScanner()]
        self.ai_service = ai_service or AISecurityService()
        self.report_generator = report_generator or SecurityReportGenerator()

    def analyze(self, source_code: str, language: str, filename: str = 'submission.py', settings_source: str = '') -> dict:
        findings, unavailable = self._run_scanners(source_code, filename, language, settings_source)
        findings = self._deduplicate(findings)
        findings = self.ai_service.enrich(findings, source_code)
        return self.report_generator.build_report(findings, unavailable)

    def _run_scanners(
        self, source_code: str, filename: str, language: str, settings_source: str = '',
    ) -> tuple[list[SecurityFinding], list[ScannerUnavailable]]:
        """Returns (findings, unavailable). `unavailable` is what keeps a
        scanner that could not run from being indistinguishable from a clean
        result - see ScannerUnavailable."""
        findings: list[SecurityFinding] = []
        unavailable: list[ScannerUnavailable] = []
        for scanner in self.scanners:
            required_language = _LANGUAGE_RESTRICTED_SCANNERS.get(scanner.name)
            if required_language and required_language != language:
                # Not applicable to this language - not a failure, and
                # deliberately not reported as unavailable.
                continue
            try:
                findings.extend(scanner.scan(source_code, filename, settings_source))
            except Exception:
                # One scanner failing (tool crash, unexpected input) must never
                # take down the whole security analysis - the others still run,
                # and a partial report beats a 500. The report now says so.
                logger.exception('Scanner "%s" raised - continuing with remaining scanners.', scanner.name)
                unavailable.append(ScannerUnavailable(
                    scanner=scanner.name, reason='error', detail='Scanner raised an unexpected error.',
                ))
                continue
            reported = scanner.consume_unavailable()
            if reported is not None:
                unavailable.append(reported)
        return findings, unavailable

    @staticmethod
    def _deduplicate(findings: list[SecurityFinding]) -> list[SecurityFinding]:
        """Two scanners can legitimately flag the same line (e.g. Bandit's
        generic import-blacklist check and a future Semgrep rule both on a
        pickle.loads call) - keep the first occurrence only, so the same
        vulnerability isn't shown/scored twice."""
        seen = set()
        unique = []
        for finding in findings:
            key = (finding.vulnerability_type, finding.line_number)
            if key in seen:
                continue
            seen.add(key)
            unique.append(finding)
        return unique
