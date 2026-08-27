"""Sends already-detected vulnerability findings (type, severity, line,
offending snippet) to the existing LLM client for explanation + remediation
text only.

Scanners (bandit_service, custom_rules_service) decide *what* is a
vulnerability; this only decides *how to describe and fix* what they already
found. The AI is never given the raw source code to hunt through on its own
and never asked "are there vulnerabilities here" - only "explain these
specific N findings". Reuses ai.client.generate_text as-is; no changes to the
existing AI integration.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ai.client import generate_text
from core.execution_budget import STAGE_AI_ENRICHMENT, BudgetExceeded

from .types import SecurityFinding

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION = (
    'You are a senior application security engineer. You will be given a numbered list of '
    'vulnerabilities that a static analysis tool has already detected - each with its category, '
    'severity, line number, and offending code snippet. Do NOT decide whether something is a '
    'vulnerability; that has already been decided. For EACH finding, write a short, plain-language '
    'explanation of why it is dangerous and a concrete, specific remediation. '
    'Respond with ONLY a JSON array, exactly one object per finding in the same order, each of the '
    'shape {"explanation": "<1-3 sentences>", "remediation": "<1-3 sentences, concrete>"}. '
    'No other text, no markdown fences.'
)


class AISecurityService:
    """Batches every finding into a single LLM call (one request per security
    scan, not one per finding) - cheaper and keeps findings from the same file
    explained with awareness of each other."""

    def __init__(self, budget=None):
        # Optional request-wide deadline (core/execution_budget.py), passed
        # only by the repository-context path via
        # SecurityAnalysisService(ai_service=...). None - every other caller -
        # keeps the unbounded chain and the identical fallback behavior.
        self._budget = budget

    def enrich(self, findings: list[SecurityFinding], source_code: str) -> list[SecurityFinding]:
        if not findings:
            return findings

        prompt = self._build_prompt(findings)
        try:
            raw = generate_text(prompt, _SYSTEM_INSTRUCTION, budget=self._budget)
            entries = self._parse_response(raw, expected_count=len(findings))
        except BudgetExceeded:
            # Caught before the broad handler below on purpose: running out of
            # time is not an AI failure. The findings themselves are real and
            # are kept - only the AI-written prose is replaced with the
            # scanner's own text, exactly as in the failure case, but recorded
            # against the budget as a skipped stage rather than logged as a
            # provider problem.
            if self._budget is not None:
                self._budget.mark_skipped(STAGE_AI_ENRICHMENT)
            logger.warning(
                'AI security enrichment skipped - request budget exhausted; '
                'using scanner-provided text.',
            )
            entries = [None] * len(findings)
        except Exception:
            logger.exception('AI security enrichment failed - falling back to scanner-provided text.')
            entries = [None] * len(findings)

        for finding, entry in zip(findings, entries):
            finding.explanation = (entry or {}).get('explanation') or self._fallback_explanation(finding)
            finding.remediation = (entry or {}).get('remediation') or self._fallback_remediation(finding)
        return findings

    @staticmethod
    def _build_prompt(findings: list[SecurityFinding]) -> str:
        blocks = [
            f'Finding {i}:\n'
            f'Category: {f.title}\n'
            f'Severity: {f.severity.value}\n'
            f'Rule: {f.rule_id}\n'
            f'Line: {f.line_number}\n'
            f'Scanner message: {f.description}\n'
            f'Code:\n{f.code_snippet}'
            for i, f in enumerate(findings, start=1)
        ]
        return '\n\n'.join(blocks)

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        text = text.strip()
        if text.startswith('```'):
            lines = text.splitlines()
            if lines and lines[0].startswith('```'):
                lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            text = '\n'.join(lines)
        return text.strip()

    @classmethod
    def _parse_response(cls, raw: str, expected_count: int) -> list[Optional[dict]]:
        try:
            data = json.loads(cls._strip_code_fences(raw))
        except json.JSONDecodeError:
            logger.warning('Could not parse AI security response as JSON.')
            return [None] * expected_count

        if not isinstance(data, list):
            return [None] * expected_count

        # Pad/truncate defensively - the model doesn't always return exactly
        # `expected_count` entries even when explicitly told the count.
        normalized = [entry if isinstance(entry, dict) else None for entry in data]
        normalized += [None] * expected_count
        return normalized[:expected_count]

    @staticmethod
    def _fallback_explanation(finding: SecurityFinding) -> str:
        return finding.description or f'{finding.title} detected by {finding.scanner}.'

    @staticmethod
    def _fallback_remediation(finding: SecurityFinding) -> str:
        return f'Review this {finding.title.lower()} finding and apply the standard secure-coding fix for this category.'
