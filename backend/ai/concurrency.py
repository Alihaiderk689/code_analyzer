"""Bounds how many synchronous, in-request AI calls can be in flight at once.

Every AI-triggering endpoint (Suggestions/Explanation/Refactor, both chat
surfaces, security-scan enrichment) calls the provider fallback chain
synchronously inside the request/response cycle - there is no Celery/async
dispatch for any of them. With gunicorn's sync worker class and a small
worker count (`backend/Dockerfile`'s `--workers 3`), each in-flight AI call
occupies one whole worker for up to ~60-80s (see `ai/client.py`'s
AI_REQUEST_TIMEOUT_SECONDS and the fallback chain). `core.throttling`'s
`AIRateThrottle`/`AnalysisCreateRateThrottle` bound *rate over time*, not
*concurrent in-flight requests* - a single user comfortably within their
per-minute allowance can still open several concurrent AI requests and, with
only a handful of workers total, starve the entire application of serving
capacity for everyone else.

Built on Django's existing cache framework (`django.core.cache.cache`,
`core.cache.ResilientRedisCache` in production - see `config/settings.py`),
not a new dependency: the same cache already backs DRF's throttles and is
already relied on for exactly this kind of cross-worker-shared state.
`cache.add()` is atomic ("set only if the key doesn't already exist"), which
is precisely the primitive a slot-acquisition semaphore needs. This module
works identically (just scoped per-process instead of cluster-wide) against
the LocMemCache fallback used in tests/local dev with no Redis configured -
no new infrastructure, paid or otherwise, is required either way.
"""
from __future__ import annotations

from contextlib import contextmanager

from django.core.cache import cache

# Exactly one in-flight AI request per user at a time - enough to let a
# single legitimate request through, not enough for one account to multiply
# its impact on shared worker capacity by opening several concurrently.
_PER_USER_KEY = 'ai_inflight_user:{user_id}'
_PER_USER_SLOT_TIMEOUT_SECONDS = 90  # comfortably above the ~60-80s worst-case chain; expires a stale slot if a worker is killed mid-request

# Caps total concurrent AI calls across *all* users below the app's total
# worker count, so at least one worker stays free for non-AI traffic (login,
# health checks, GitHub webhook acks, ...) even if every AI slot is in use.
_GLOBAL_SLOT_COUNT = 2
_GLOBAL_SLOT_KEYS = tuple(f'ai_inflight_global:{i}' for i in range(_GLOBAL_SLOT_COUNT))
_GLOBAL_SLOT_TIMEOUT_SECONDS = 90


class AICapacityExhausted(Exception):
    """No concurrency slot was available. Callers map this to an immediate
    429 - queuing/blocking for a slot would itself tie up the very worker
    this mechanism exists to protect, defeating the point."""


@contextmanager
def ai_concurrency_slot(user_id):
    """Acquires a per-user + global slot for the duration of the `with`
    block, raising AICapacityExhausted immediately if either is unavailable.
    Always releases whatever it acquired, including on an exception from the
    wrapped AI call."""
    per_user_key = _PER_USER_KEY.format(user_id=user_id)
    if not cache.add(per_user_key, 1, timeout=_PER_USER_SLOT_TIMEOUT_SECONDS):
        raise AICapacityExhausted('You already have an AI request in progress. Please wait for it to finish.')

    global_key = None
    try:
        for candidate in _GLOBAL_SLOT_KEYS:
            if cache.add(candidate, 1, timeout=_GLOBAL_SLOT_TIMEOUT_SECONDS):
                global_key = candidate
                break
        if global_key is None:
            raise AICapacityExhausted('The AI service is at capacity right now. Please try again in a moment.')
        yield
    finally:
        cache.delete(per_user_key)
        if global_key is not None:
            cache.delete(global_key)
