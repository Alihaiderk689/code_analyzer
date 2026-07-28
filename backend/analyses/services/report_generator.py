"""Turns a list of (AI-enriched) SecurityFinding objects into the final report
dict returned by the API and cached on Analysis.security_report: overall
score, risk level, severity breakdown, and the vulnerability list shaped for
the frontend (score/risk badge/vulnerability cards/expandable details - see
analyses/serializers.py's SecurityReportSerializer for the exact contract).
"""
from __future__ import annotations

from .types import RiskLevel, SecurityFinding, Severity

STARTING_SCORE = 100

SEVERITY_PENALTIES: dict[Severity, int] = {
    Severity.CRITICAL: 30,
    Severity.HIGH: 20,
    Severity.MEDIUM: 10,
    Severity.LOW: 5,
}

_SEVERITY_SORT_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}

# Score -> risk-level thresholds. A single critical finding (100 - 30 = 70)
# lands as MEDIUM rather than something scarier-sounding; two criticals or a
# critical plus a high (100 - 50 = 50) is HIGH; anything that guts the score
# below 40 is CRITICAL overall.
_RISK_LEVEL_THRESHOLDS: list[tuple[int, RiskLevel]] = [
    (40, RiskLevel.CRITICAL),
    (60, RiskLevel.HIGH),
    (80, RiskLevel.MEDIUM),
    (95, RiskLevel.LOW),
]


class SecurityReportGenerator:
    def build_report(self, findings: list[SecurityFinding]) -> dict:
        score = self._score(findings)
        return {
            'score': score,
            'risk_level': self._risk_level(score).value,
            'summary': self._summary(findings),
            'vulnerabilities': [f.to_dict() for f in self._sorted(findings)],
        }

    @staticmethod
    def _score(findings: list[SecurityFinding]) -> int:
        penalty = sum(SEVERITY_PENALTIES.get(f.severity, 0) for f in findings)
        return max(0, STARTING_SCORE - penalty)

    @staticmethod
    def _risk_level(score: int) -> RiskLevel:
        for threshold, level in _RISK_LEVEL_THRESHOLDS:
            if score < threshold:
                return level
        return RiskLevel.MINIMAL

    @staticmethod
    def _summary(findings: list[SecurityFinding]) -> dict:
        counts = {severity.value: 0 for severity in Severity}
        for finding in findings:
            counts[finding.severity.value] += 1
        counts['total'] = len(findings)
        return counts

    @staticmethod
    def _sorted(findings: list[SecurityFinding]) -> list[SecurityFinding]:
        return sorted(
            findings,
            key=lambda f: (_SEVERITY_SORT_ORDER.get(f.severity, 4), f.line_number or 0),
        )
