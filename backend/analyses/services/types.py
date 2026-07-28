"""Shared types for the security-analysis scanner pipeline.

bandit_service, custom_rules_service, and any future scanner (Semgrep is the
one explicitly planned) all speak this one structure, so security_service can
aggregate results from an arbitrary list of scanners without caring which
tool produced a given finding. Adding a new scanner later means writing a
class that implements BaseSecurityScanner and appending it to the list in
SecurityAnalysisService.__init__ - nothing else in the pipeline changes.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    CRITICAL = 'critical'
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'


class RiskLevel(str, Enum):
    CRITICAL = 'critical'
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'
    MINIMAL = 'minimal'


class VulnerabilityType(str, Enum):
    SQL_INJECTION = 'sql_injection'
    HARDCODED_SECRET = 'hardcoded_secret'
    HARDCODED_PASSWORD = 'hardcoded_password'
    COMMAND_INJECTION = 'command_injection'
    UNSAFE_DESERIALIZATION = 'unsafe_deserialization'
    WEAK_RANDOMNESS = 'weak_randomness'
    PATH_TRAVERSAL = 'path_traversal'
    MISSING_AUTHENTICATION = 'missing_authentication'
    MISSING_AUTHORIZATION = 'missing_authorization'
    XSS = 'xss'
    CSRF_DISABLED = 'csrf_disabled'
    DEBUG_ENABLED = 'debug_enabled'
    WEAK_PASSWORD_STORAGE = 'weak_password_storage'
    SENSITIVE_DATA_EXPOSURE = 'sensitive_data_exposure'
    UNSAFE_FILE_UPLOAD = 'unsafe_file_upload'
    OTHER = 'other'


# Human-readable labels, shared across scanners so a finding's `title` reads
# the same regardless of which tool (Bandit, custom rules, future Semgrep)
# actually detected it.
VULNERABILITY_TYPE_LABELS: dict[VulnerabilityType, str] = {
    VulnerabilityType.SQL_INJECTION: 'SQL Injection',
    VulnerabilityType.HARDCODED_SECRET: 'Hardcoded Secret',
    VulnerabilityType.HARDCODED_PASSWORD: 'Hardcoded Password',
    VulnerabilityType.COMMAND_INJECTION: 'Command Injection',
    VulnerabilityType.UNSAFE_DESERIALIZATION: 'Unsafe Deserialization',
    VulnerabilityType.WEAK_RANDOMNESS: 'Weak Randomness',
    VulnerabilityType.PATH_TRAVERSAL: 'Path Traversal',
    VulnerabilityType.MISSING_AUTHENTICATION: 'Missing Authentication',
    VulnerabilityType.MISSING_AUTHORIZATION: 'Missing Authorization',
    VulnerabilityType.XSS: 'Cross-Site Scripting (XSS)',
    VulnerabilityType.CSRF_DISABLED: 'CSRF Protection Disabled',
    VulnerabilityType.DEBUG_ENABLED: 'Debug Mode Enabled',
    VulnerabilityType.WEAK_PASSWORD_STORAGE: 'Weak Password Storage',
    VulnerabilityType.SENSITIVE_DATA_EXPOSURE: 'Sensitive Data Exposure',
    VulnerabilityType.UNSAFE_FILE_UPLOAD: 'Unsafe File Upload',
    VulnerabilityType.OTHER: 'Other',
}


@dataclass
class SecurityFinding:
    """One vulnerability finding, in the shape every scanner must produce.
    `explanation`/`remediation` start empty - only ai_security_service fills
    those in, and only for findings scanners already detected; the AI is never
    asked to decide whether something is a vulnerability in the first place."""

    scanner: str
    rule_id: str
    vulnerability_type: VulnerabilityType
    severity: Severity
    description: str
    line_number: Optional[int]
    code_snippet: str
    confidence: Optional[str] = None
    explanation: Optional[str] = None
    remediation: Optional[str] = None

    @property
    def title(self) -> str:
        return VULNERABILITY_TYPE_LABELS.get(self.vulnerability_type, self.vulnerability_type.value)

    @property
    def id(self) -> str:
        return f'{self.scanner}:{self.rule_id}:{self.line_number or 0}'

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'scanner': self.scanner,
            'rule_id': self.rule_id,
            'vulnerability_type': self.vulnerability_type.value,
            'severity': self.severity.value,
            'title': self.title,
            'description': self.description,
            'line_number': self.line_number,
            'code_snippet': self.code_snippet,
            'confidence': self.confidence,
            'explanation': self.explanation,
            'remediation': self.remediation,
        }


class BaseSecurityScanner(abc.ABC):
    """Interface every scanner implements - Bandit today, custom rules for
    what Bandit can't see, Semgrep whenever it's added. SecurityAnalysisService
    only ever calls `.scan()`; it never knows or cares which subclass it's
    talking to."""

    name: str = 'base'

    @abc.abstractmethod
    def scan(self, source_code: str, filename: str = 'submission.py', settings_source: str = '') -> list[SecurityFinding]:
        """Must not raise for scanner-internal failures (bad input, tool
        crash/missing binary, timeout) - log and return [] instead, so one
        scanner failing never takes down the whole security analysis.

        `settings_source` is the analyzed repo's Django settings file content,
        when one could be found (see pr_analysis_service._find_settings_source) -
        best-effort project context for scanners that need it (custom_rules'
        missing-auth check); '' means "not available/not applicable", which
        every scanner must treat the same as not having been passed at all."""
        raise NotImplementedError
