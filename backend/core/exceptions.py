"""Project-wide DRF exception handler.

DRF's default exception_handler already turns APIException/Http404/PermissionDenied
into clean JSON responses - that part is left alone. What it does NOT do is catch
anything else (a bug, an unexpected third-party client error, a DB hiccup): those
propagate past DRF and Django renders its own HTML 500 page. For a JSON API that's
both useless to the frontend (safeJson() in api.js can't parse it) and, in DEBUG,
a way to leak a full traceback. This handler closes that gap: log every unhandled
exception with a traceback, then always return a small JSON body instead.
"""
import logging

from django.conf import settings
from django.db import InterfaceError, OperationalError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.exceptions import Throttled

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if response is not None:
        if isinstance(exc, Throttled):
            wait = exc.wait
            detail = (
                f'Too many requests. Try again in {int(wait)} second(s).'
                if wait is not None else 'Too many requests. Please slow down.'
            )
            response.data = {'detail': detail}
        return response

    request = context.get('request')

    # A dropped connection or an exhausted pooler is a dependency outage, not a
    # bug in this code: 503 tells the client to retry, where the generic 500
    # below implies "we broke, retrying won't help". Deliberately NO automatic
    # retry here - retrying into an exhausted pooler is what turns a blip into
    # an outage. Logged at error (not exception) since the traceback is always
    # the same connection-teardown stack and carries no diagnostic value.
    if isinstance(exc, (OperationalError, InterfaceError)):
        logger.error(
            'database_unavailable',
            extra={
                'path': getattr(request, 'path', None),
                'method': getattr(request, 'method', None),
                'error': exc.__class__.__name__,
            },
        )
        return Response(
            {'detail': 'Service temporarily unavailable. Please try again shortly.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    logger.exception(
        'unhandled_exception',
        extra={'path': getattr(request, 'path', None), 'method': getattr(request, 'method', None)},
    )

    if settings.DEBUG:
        return Response({'detail': str(exc) or exc.__class__.__name__, 'exception': exc.__class__.__name__}, status=500)
    return Response({'detail': 'An unexpected error occurred. Please try again later.'}, status=500)
