"""Per-user daily quota for 'Chat with Your Code' messages, global across every
analysis/conversation the user has (not per-conversation) - otherwise the limit
would be trivially dodged by starting a new analysis. There's no separate
counter model: usage is derived directly from ChatMessage timestamps, so the
limit self-maintains with no extra state to keep in sync.

Resets at local midnight on the user's own device, not a rolling 24h window -
using your last message at 9pm gives you all 3 back at midnight, not 9pm the
next day. The server has no idea what timezone a user is in on its own, so the
client reports its UTC offset (JS Date.getTimezoneOffset() convention: UTC
minus local, in minutes - e.g. -300 for UTC+5) with every relevant request.
"""
from datetime import timedelta

from django.utils import timezone

from .models import ChatMessage

DAILY_MESSAGE_LIMIT = 3

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


def get_rate_limit_status(user, tz_offset_minutes=0, now=None):
    """Returns {limit, used, remaining, reset_at}. `reset_at` is the next local
    midnight (only set once the quota is exhausted) - what the frontend counts
    down to. `now` is an override for tests - defaults to the real current time."""
    window_start = _local_midnight_boundary(tz_offset_minutes, now)
    used = ChatMessage.objects.filter(
        conversation__analysis__owner=user,
        role=ChatMessage.Role.USER,
        created_at__gte=window_start,
    ).count()
    remaining = max(0, DAILY_MESSAGE_LIMIT - used)
    reset_at = window_start + timedelta(hours=24) if used >= DAILY_MESSAGE_LIMIT else None
    return {'limit': DAILY_MESSAGE_LIMIT, 'used': used, 'remaining': remaining, 'reset_at': reset_at}
