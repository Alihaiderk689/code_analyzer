"""Health endpoints.

Two of them, deliberately, because the platform's restart trigger and an
operator's "is the system actually working" question need different answers:

- health_check (liveness) checks nothing but that this process can serve a
  request. It is what render.yaml's healthCheckPath points at. If it probed
  the database, a transient Postgres blip would fail the check and make Render
  restart every container - which cannot fix a database problem and turns a
  brief outage into a restart loop on top of it.

- readiness_check probes the dependencies a request actually needs and returns
  503 when one is down. Nothing restarts on it; it exists so monitoring and
  operators get a truthful answer instead of the flat "ok" that stayed green
  through a total database outage.
"""
import logging

from django.core.cache import cache
from django.db import OperationalError, connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([AllowAny])
def health_check(request):
    """Liveness. Always 200 while the process is up - see module docstring."""
    return Response({'status': 'ok'})


def _check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return True, None
    except (OperationalError, Exception) as exc:  # noqa: BLE001 - any driver error means "not ready"
        return False, exc.__class__.__name__


def _check_cache():
    """Cache failure is reported but does NOT make the service unready: the
    cache backs DRF throttle counters, which fail open (see
    core.cache.ResilientRedisCache), so requests still succeed without it."""
    try:
        cache.set('healthcheck', 'ok', 5)
        return cache.get('healthcheck') == 'ok', None
    except Exception as exc:  # noqa: BLE001
        return False, exc.__class__.__name__


@api_view(['GET'])
@permission_classes([AllowAny])
def readiness_check(request):
    """Readiness. 200 only when the database is reachable; 503 otherwise.

    Deliberately not wired to render.yaml's healthCheckPath - see the module
    docstring for why probing dependencies from the restart trigger is worse
    than the outage it would report.
    """
    db_ok, db_error = _check_database()
    cache_ok, cache_error = _check_cache()

    checks = {
        'database': {'ok': db_ok, **({'error': db_error} if db_error else {})},
        # 'degraded' rather than a hard failure - throttling falls back to
        # fail-open, everything else is unaffected.
        'cache': {'ok': cache_ok, **({'error': cache_error} if cache_error else {})},
    }

    if not db_ok:
        logger.error('readiness_check.database_unavailable', extra={'error': db_error})
        return Response(
            {'status': 'unavailable', 'checks': checks},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({'status': 'ok' if cache_ok else 'degraded', 'checks': checks})
