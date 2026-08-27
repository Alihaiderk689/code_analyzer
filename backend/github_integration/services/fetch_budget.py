"""A shared, monotonic wall-clock deadline for the several GitHub fetches one
repository-context analysis makes.

Per-request timeouts (github_client.REQUEST_TIMEOUT_SECONDS) bound each call
in isolation, but they say nothing about the *sum*: the context check makes up
to 11 fetches, so 11 x 15s = 165s against gunicorn's `--timeout 120`. This adds
the missing total bound - one deadline, taken from time.monotonic() (immune to
clock steps/NTP, unlike time.time()), shared by every fetch in the phase.

Deliberately opt-in: only callers that pass a FetchBudget to GitHubClient are
affected, so PR review, OAuth, repo listing and index building keep their
existing unbudgeted behavior.
"""
from __future__ import annotations

from core.execution_budget import BudgetExceeded, ExecutionBudget

# Reason string surfaced to the API/DB when a context check stopped early.
# A named constant so callers can distinguish "we ran out of time" from a
# GitHub auth error, a rate limit, or a genuine fetch failure.
TRUNCATED_BUDGET_EXHAUSTED = 'fetch_budget_exhausted'

# Below this, whatever is left of the budget is too small to be a useful
# request timeout: the call would almost certainly trip its own connect
# timeout and surface as a bogus "Network error calling GitHub API" rather
# than the honest "we ran out of budget". Treated as exhausted instead.
MIN_REQUEST_SLICE_SECONDS = 1.0


class FetchBudgetExceeded(BudgetExceeded):
    """Raised when a GitHub fetch is attempted with no budget left.

    Deliberately *not* a GitHubAPIError subclass: every caller already treats
    GitHubAPIError as "GitHub said no" (auth / rate limit / genuine fetch
    failure), and this is none of those - it is our own deadline. Keeping it a
    separate type is what stops it being silently reclassified as a fetch
    failure by the existing `except GitHubAPIError` handlers.

    A BudgetExceeded subclass so a caller holding the request-wide budget can
    catch both kinds of "we ran out of time" in one place if it wants to,
    while the fetch phase stays separately identifiable.
    """


class FetchBudget(ExecutionBudget):
    """Total time allowance for one repository-context GitHub fetch phase.

    Behavior is unchanged from when this was standalone; the monotonic
    deadline, MIN_REQUEST_SLICE_SECONDS floor and clamping logic now live in
    core.execution_budget.ExecutionBudget, shared with the request-wide
    budget that bounds the analysis stages after this phase.
    """

    min_slice_seconds = MIN_REQUEST_SLICE_SECONDS
    log_event = 'github_fetch_budget.exhausted'

    def slice_for(self, per_request_timeout: float, stage: str) -> float:
        """The timeout to hand a single request: the normal per-request value,
        clamped so no one call can outlive the shared deadline. Raises
        FetchBudgetExceeded if there is nothing usable left."""
        if self.expired(stage):
            raise FetchBudgetExceeded(
                f'Repository-context fetch budget of {self.total_seconds:g}s exhausted at {stage}.'
            )
        return min(per_request_timeout, self.remaining())
