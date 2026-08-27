"""A monotonic wall-clock deadline shared by every expensive stage of one
synchronous request.

github_integration/services/fetch_budget.py already bounds the *GitHub fetch*
phase of a repository-context analysis. This generalizes the same idea to the
stages that run after it - sandboxed execution, Bandit, and the AI provider
fallback chain - because those are per-file and the context check runs them
up to seven times (the primary file plus up to six dependency-graph
neighbors). Per-stage timeouts bound each invocation; only a shared deadline
bounds their sum.

Design notes:

- `time.monotonic()`, not `time.time()`: immune to NTP steps and DST.
- Opt-in everywhere. Every consumer takes `budget=None` and, when it is None,
  behaves exactly as it did before - so the PR-review pipeline, the manual
  paste/upload analysis, the security-scan endpoint and the chat surfaces are
  untouched. Only the repository-context request creates a budget.
- Stages are *skipped* when they cannot be afforded, not squeezed into a
  meaningless slice. A Bandit run cut to 0.5s reports "Bandit timed out",
  which is a lie about the scanner; refusing to start it and saying
  "budget_exhausted" is the truth. See MIN_* constants at each call site.
- The budget itself accumulates what it caused to be skipped
  (`degraded_stages`), so the caller can report degradation without any of
  the intermediate services growing a new return-value contract.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Stage identifiers - shared so a skipped stage is named the same in logs, in
# the API response, and in the persisted row.
STAGE_GITHUB_FETCH = 'github_fetch'
STAGE_RUNTIME_CHECK = 'runtime_check'
STAGE_BANDIT = 'bandit'
STAGE_AI_ENRICHMENT = 'ai_enrichment'
STAGE_RELATED_FILES = 'related_files'

# Truncation/degradation reason for the request-wide budget. Deliberately
# distinct from fetch_budget.TRUNCATED_BUDGET_EXHAUSTED ('fetch_budget_exhausted'):
# "GitHub was too slow" and "the whole request ran out of time" are different
# operational problems with different fixes.
REASON_REQUEST_BUDGET_EXHAUSTED = 'request_budget_exhausted'


class BudgetExceeded(Exception):
    """Raised when an expensive stage is attempted with no budget left.

    Deliberately a plain Exception subclass and *not* related to
    GitHubAPIError or to any provider/scanner error type. Every layer this
    passes through already has broad `except Exception` handlers that mean
    "this provider/scanner failed, degrade gracefully"; keeping this a
    distinct type is what lets those handlers re-raise it instead of
    reporting our own deadline as a GitHub outage, an AI provider failure or
    a scanner crash.
    """


class ExecutionBudget:
    """Total time allowance for one request, shared across its stages."""

    # Below this there is not enough left for any stage to be worth starting.
    min_slice_seconds: float = 1.0
    # Structured-log event name; subclasses override so operators can grep for
    # the specific phase that ran out.
    log_event: str = 'execution_budget.exhausted'

    def __init__(self, total_seconds: float):
        self.total_seconds = float(total_seconds)
        self._deadline = time.monotonic() + self.total_seconds
        self.exhausted = False
        self._skipped: list[str] = []

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def expired(self, stage: str) -> bool:
        """True once there isn't enough left for another worthwhile call.
        Marks (and logs) the budget as exhausted the first time."""
        if self.remaining() < self.min_slice_seconds:
            self.mark_exhausted(stage)
            return True
        return False

    def can_afford(self, seconds: float, stage: str) -> bool:
        """True if `stage`, whose minimum useful cost is `seconds`, can still
        be run. When it cannot, the stage is recorded as skipped and the
        budget is marked exhausted - there is no point pretending otherwise
        when the cheapest remaining work no longer fits."""
        if self.remaining() >= seconds:
            return True
        self.mark_skipped(stage)
        self.mark_exhausted(stage)
        return False

    def slice_for(self, default: float, stage: str) -> float:
        """The timeout to hand a single call: its normal value, clamped so it
        cannot outlive the shared deadline. Raises BudgetExceeded when there
        is nothing usable left."""
        if self.expired(stage):
            raise BudgetExceeded(
                f'{type(self).__name__} of {self.total_seconds:g}s exhausted at {stage}.'
            )
        return min(default, self.remaining())

    def mark_skipped(self, stage: str) -> None:
        """Record that `stage` did not run because of this budget. Ordered and
        deduplicated: the same stage skipped on six neighbors is one entry."""
        if stage not in self._skipped:
            self._skipped.append(stage)

    def mark_exhausted(self, stage: str) -> None:
        if not self.exhausted:
            self.exhausted = True
            logger.warning(
                self.log_event,
                extra={'stage': stage, 'budget_seconds': self.total_seconds},
            )

    @property
    def degraded_stages(self) -> list[str]:
        return list(self._skipped)


class RequestBudget(ExecutionBudget):
    """The request-wide budget for one repository-context analysis."""

    log_event = 'request_budget.exhausted'
