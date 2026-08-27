"""A Redis cache backend that degrades instead of failing the request.

Django's built-in RedisCache propagates connection errors. That is the wrong
behavior for what this cache is actually used for: DRF throttle counters, read
on the way into *every* request. With a raising backend, Redis going down turns
into a 500 on every endpoint - the cache becoming a hard dependency of the
whole API, which it never was when it was per-process LocMemCache.

So this backend swallows redis connection/timeout errors and behaves as a cache
miss. The security consequence is explicit and deliberate: while Redis is
unavailable, DRF throttles FAIL OPEN (get() returns None -> throttle sees no
history -> request allowed). That is the right trade here because throttling is
a rate control, not an authorization control, and because the protections that
actually stop brute force do not depend on this cache:

  - OTP guessing is bounded by Profile.otp_attempts, a database row guarded by
    select_for_update (accounts/otp.py) - unaffected by Redis.
  - Chat and GitHub file-check daily quotas are derived from database rows, not
    cache counters - unaffected by Redis.
  - Password checks, JWT validation, CSRF and ownership scoping never touch the
    cache at all.

Failures are logged (not silently swallowed) so a degraded cache is visible,
and readiness_check reports cache state separately from database state.
"""
import logging

from django.core.cache.backends.redis import RedisCache

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import shape depends on the installed redis version
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover
    RedisError = Exception


class ResilientRedisCache(RedisCache):
    """RedisCache that treats a Redis outage as a cache miss."""

    def _degrade(self, operation, exc, default=None):
        logger.warning(
            'cache.redis_unavailable',
            extra={'operation': operation, 'error': exc.__class__.__name__},
        )
        return default

    def get(self, key, default=None, version=None):
        try:
            return super().get(key, default, version)
        except RedisError as exc:
            return self._degrade('get', exc, default)

    def set(self, key, value, timeout=None, version=None, client=None):
        try:
            return super().set(key, value, timeout, version)
        except RedisError as exc:
            return self._degrade('set', exc)

    def add(self, key, value, timeout=None, version=None):
        try:
            return super().add(key, value, timeout, version)
        except RedisError as exc:
            # False = "not added"; callers treat that as "someone else has it",
            # which is the safe reading when we cannot tell.
            return self._degrade('add', exc, False)

    def delete(self, key, version=None):
        try:
            return super().delete(key, version)
        except RedisError as exc:
            return self._degrade('delete', exc, False)

    def touch(self, key, timeout=None, version=None):
        try:
            return super().touch(key, timeout, version)
        except RedisError as exc:
            return self._degrade('touch', exc, False)

    def incr(self, key, delta=1, version=None):
        try:
            return super().incr(key, delta, version)
        except RedisError as exc:
            return self._degrade('incr', exc)

    def get_many(self, keys, version=None):
        try:
            return super().get_many(keys, version)
        except RedisError as exc:
            return self._degrade('get_many', exc, {})

    def set_many(self, data, timeout=None, version=None):
        try:
            return super().set_many(data, timeout, version)
        except RedisError as exc:
            return self._degrade('set_many', exc, list(data))

    def clear(self):
        try:
            return super().clear()
        except RedisError as exc:
            return self._degrade('clear', exc)
