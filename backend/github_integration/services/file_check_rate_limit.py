"""Per-user daily quota for on-demand 'analyze this file' checks (see
pr_analysis_service.PRAnalysisService.analyze_file_by_path) - global across
every monitored repository, not per-repository, matching the "one repo
monitored at a time" model. Mirrors chat/rate_limit.py's exact approach: no
separate counter model, usage is derived directly from RepositoryFileCheck
timestamps, so the limit self-maintains with no extra state to keep in sync.

Resets at local midnight on the user's own device, not a rolling 24h window -
see chat/rate_limit.py's docstring for the full reasoning, identical here.
"""
from datetime import timedelta

from django.utils import timezone

from ..models import RepositoryFileCheck

DAILY_FILE_CHECK_LIMIT = 1

# Real-world UTC offsets run from UTC-12 to UTC+14; clamp so a malformed or
# malicious value can't shift the day boundary somewhere nonsensical.
_MIN_OFFSET_MINUTES = -14 * 60
_MAX_OFFSET_MINUTES = 12 * 60


def _clamp_offset(tz_offset_minutes):
    try:
        offset = int(tz_offset_minutes)
    except (TypeError, ValueError):
        return 0
    return max(_MIN_OFFSET_MINUTES, min(_MAX_OFFSET_MINUTES, offset))


def _local_midnight_boundary(tz_offset_minutes, now=None):
    """The UTC instant corresponding to the most recent local midnight for a
    client at the given UTC offset."""
    offset = _clamp_offset(tz_offset_minutes)
    now = now or timezone.now()
    local_now = now - timedelta(minutes=offset)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight + timedelta(minutes=offset)


def get_file_check_status(user, tz_offset_minutes=0, now=None):
    """Returns {limit, used, remaining, reset_at, today_check}. `reset_at` is
    the next local midnight (only set once the quota is exhausted). `today_check`
    is today's RepositoryFileCheck row if one exists, or None - re-viewing the
    same file you already checked today is free (no new GitHub/Groq call, see
    the view), only checking a *different* file is blocked."""
    window_start = _local_midnight_boundary(tz_offset_minutes, now)
    todays_checks = RepositoryFileCheck.objects.filter(
        user=user, created_at__gte=window_start,
    ).select_related('analysis')
    used = todays_checks.count()
    remaining = max(0, DAILY_FILE_CHECK_LIMIT - used)
    reset_at = window_start + timedelta(hours=24) if used >= DAILY_FILE_CHECK_LIMIT else None
    return {
        'limit': DAILY_FILE_CHECK_LIMIT,
        'used': used,
        'remaining': remaining,
        'reset_at': reset_at,
        'today_check': todays_checks.order_by('-created_at').first(),
    }
