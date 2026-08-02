from django.test import TestCase, override_settings

from .exceptions import custom_exception_handler


class CustomExceptionHandlerTests(TestCase):
    """DRF's default exception_handler returns None for anything that isn't an
    APIException/Http404/PermissionDenied - that's the gap custom_exception_handler
    closes, so every unhandled exception still gets a JSON body instead of
    Django's HTML 500 page."""

    def test_unhandled_exception_returns_generic_500_json(self):
        with override_settings(DEBUG=False):
            response = custom_exception_handler(RuntimeError('boom'), {'request': None})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data, {'detail': 'An unexpected error occurred. Please try again later.'})

    def test_unhandled_exception_includes_detail_in_debug(self):
        with override_settings(DEBUG=True):
            response = custom_exception_handler(RuntimeError('boom'), {'request': None})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data['detail'], 'boom')
        self.assertEqual(response.data['exception'], 'RuntimeError')

    def test_known_drf_exceptions_still_handled_normally(self):
        from rest_framework.exceptions import NotFound

        response = custom_exception_handler(NotFound('missing'), {'request': None})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'missing')
