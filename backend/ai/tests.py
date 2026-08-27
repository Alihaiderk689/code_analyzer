from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ai import client
from ai.client import generate_text
from analyses.models import Analysis

User = get_user_model()


def make_user(email='user@example.com'):
    return User.objects.create_user(username=email.split('@')[0], email=email, password='TestPass123!')


class ChatViewTests(APITestCase):
    url = reverse('ai-chat')

    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(user=self.user)

    def test_requires_authentication(self):
        anon = self.client_class()
        response = anon.post(self.url, {'message': 'hi'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty_message_rejected(self):
        response = self.client.post(self.url, {'message': '   '})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_message_over_length_cap_rejected(self):
        response = self.client.post(self.url, {'message': 'x' * 8001})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_item_over_length_cap_rejected(self):
        """Each history entry is client-supplied and must be bounded the same
        way the top-level message is - otherwise a client could bypass the
        message cap by stuffing an oversized 'message' into history instead."""
        response = self.client.post(self.url, {
            'message': 'hi',
            'history': [{'role': 'user', 'content': 'x' * 8001}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('ai.views.generate_chat_reply', return_value='Hello there.')
    def test_successful_reply(self, mock_generate):
        response = self.client.post(self.url, {'message': 'hi'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['reply'], 'Hello there.')
        mock_generate.assert_called_once()

    @patch('ai.views.generate_chat_reply', side_effect=RuntimeError('groq down'))
    def test_ai_service_failure_returns_503(self, _mock_generate):
        response = self.client.post(self.url, {'message': 'hi'})
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('ai.views.generate_chat_reply', return_value='ok')
    def test_analysis_id_scoped_to_owner(self, _mock_generate):
        other_user = make_user(email='other@example.com')
        analysis = Analysis.objects.create(
            owner=other_user, name='a.py', language='Python', status=Analysis.Status.COMPLETED,
        )
        response = self.client.post(self.url, {'message': 'hi', 'analysis_id': analysis.id})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('ai.views.generate_chat_reply', return_value='ok')
    def test_own_analysis_id_included_as_context(self, mock_generate):
        analysis = Analysis.objects.create(
            owner=self.user, name='a.py', language='Python', status=Analysis.Status.COMPLETED,
        )
        response = self.client.post(self.url, {'message': 'hi', 'analysis_id': analysis.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        system_instruction = mock_generate.call_args.args[2]
        self.assertIn('a.py', system_instruction)


class AIProviderFallbackTests(SimpleTestCase):
    """ai.client tries Groq, then Gemini, then OpenRouter, in order - moving
    on to the next provider on any failure, not just a rate limit."""

    @patch('ai.client._call_openrouter')
    @patch('ai.client._call_gemini')
    @patch('ai.client._call_groq', return_value='groq reply')
    def test_uses_groq_when_it_succeeds(self, mock_groq, mock_gemini, mock_openrouter):
        self.assertEqual(generate_text('hi'), 'groq reply')
        mock_groq.assert_called_once()
        mock_gemini.assert_not_called()
        mock_openrouter.assert_not_called()

    @patch('ai.client._call_openrouter')
    @patch('ai.client._call_gemini', return_value='gemini reply')
    @patch('ai.client._call_groq', side_effect=RuntimeError('groq rate limit reached'))
    def test_falls_back_to_gemini_when_groq_fails(self, mock_groq, mock_gemini, mock_openrouter):
        self.assertEqual(generate_text('hi'), 'gemini reply')
        mock_groq.assert_called_once()
        mock_gemini.assert_called_once()
        mock_openrouter.assert_not_called()

    @patch('ai.client._call_openrouter', return_value='openrouter reply')
    @patch('ai.client._call_gemini', side_effect=RuntimeError('gemini rate limit reached'))
    @patch('ai.client._call_groq', side_effect=RuntimeError('groq rate limit reached'))
    def test_falls_back_to_openrouter_when_groq_and_gemini_fail(self, mock_groq, mock_gemini, mock_openrouter):
        self.assertEqual(generate_text('hi'), 'openrouter reply')
        mock_groq.assert_called_once()
        mock_gemini.assert_called_once()
        mock_openrouter.assert_called_once()

    @patch('ai.client._call_openrouter', side_effect=RuntimeError('openrouter down'))
    @patch('ai.client._call_gemini', side_effect=RuntimeError('gemini down'))
    @patch('ai.client._call_groq', side_effect=RuntimeError('groq down'))
    def test_raises_when_all_providers_fail(self, mock_groq, mock_gemini, mock_openrouter):
        with self.assertRaisesMessage(RuntimeError, 'openrouter down'):
            generate_text('hi')


class ProviderTimeoutBoundingTests(SimpleTestCase):
    """The fallback chain only helps if the request survives long enough to
    reach the next provider. Groq previously had no explicit timeout, so the
    SDK defaults (60s read x 2 retries) could consume ~3 minutes on their own -
    well past gunicorn's kill, so the client got a 502 instead of the intended
    503 and the fallback never ran."""

    def setUp(self):
        client._groq_client = None

    def tearDown(self):
        client._groq_client = None

    @override_settings(GROQ_API_KEY='test-key', AI_REQUEST_TIMEOUT_SECONDS=25)
    def test_groq_client_is_constructed_with_an_explicit_bound(self):
        with patch('ai.client.Groq') as groq_cls:
            client._get_groq_client()

        _, kwargs = groq_cls.call_args
        self.assertEqual(kwargs['timeout'], 25)
        # Retrying is the fallback chain's job; SDK-level retries multiply the
        # timeout invisibly.
        self.assertEqual(kwargs['max_retries'], 0)

    @override_settings(GEMINI_API_KEY='k', AI_REQUEST_TIMEOUT_SECONDS=25)
    def test_gemini_uses_the_shared_timeout(self):
        with patch('ai.client.requests.post') as post:
            post.return_value.json.return_value = {
                'candidates': [{'content': {'parts': [{'text': 'hi'}]}}]
            }
            client._call_gemini([{'role': 'user', 'content': 'x'}])

        self.assertEqual(post.call_args.kwargs['timeout'], 25)

    @override_settings(GEMINI_API_KEY='super-secret-gemini-key')
    def test_gemini_key_is_sent_as_a_header_never_in_the_url(self):
        with patch('ai.client.requests.post') as post:
            post.return_value.json.return_value = {
                'candidates': [{'content': {'parts': [{'text': 'hi'}]}}]
            }
            client._call_gemini([{'role': 'user', 'content': 'x'}])

        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'super-secret-gemini-key')
        # No query params at all, and nothing key-shaped in the URL: requests
        # builds HTTPError's message from the full URL, so a key in the query
        # string ends up in every logged provider failure.
        self.assertNotIn('params', kwargs)
        self.assertNotIn('super-secret-gemini-key', post.call_args.args[0])

    @staticmethod
    def _sending_a_real_401():
        """Patches the transport, not requests.post, so `requests` really does
        prepare the URL (encoding any query params into it) and bind it to the
        response. Mocking requests.post instead would let a test set
        response.url by hand and pass no matter where the key was put."""
        def send(_session, prepared, **_kwargs):
            response = requests.Response()
            response.status_code = 401
            response.reason = 'Unauthorized'
            response.url = prepared.url
            response.request = prepared
            response._content = b'{"error": "bad key"}'
            return response

        return patch('requests.sessions.Session.send', send)

    @override_settings(GEMINI_API_KEY='super-secret-gemini-key')
    def test_a_gemini_http_error_does_not_carry_the_key(self):
        """The regression this fix exists for: requests.HTTPError renders as
        '<status> Client Error: <reason> for url: <full url>'. With the key in
        the query string that message - and so the fallback chain's
        exc_info=True warning - contained the key verbatim."""
        with self._sending_a_real_401():
            with self.assertRaises(requests.HTTPError) as ctx:
                client._call_gemini([{'role': 'user', 'content': 'x'}])

        self.assertNotIn('super-secret-gemini-key', str(ctx.exception))
        self.assertNotIn('super-secret-gemini-key', repr(ctx.exception))
        self.assertNotIn('super-secret-gemini-key', ctx.exception.response.url)

    @override_settings(GEMINI_API_KEY='super-secret-gemini-key', GROQ_API_KEY='', OPENROUTER_API_KEY='')
    def test_the_key_never_reaches_the_provider_failure_log(self):
        """End-to-end on the actual leak path: a Gemini failure inside
        _call_with_fallback, which logs it with exc_info=True."""
        with self._sending_a_real_401():
            with self.assertLogs('ai.client', level='WARNING') as logs:
                with self.assertRaises(Exception):
                    client._call_with_fallback([{'role': 'user', 'content': 'x'}])

        self.assertNotIn('super-secret-gemini-key', '\n'.join(logs.output))

    @override_settings(OPENROUTER_API_KEY='k', AI_REQUEST_TIMEOUT_SECONDS=25)
    def test_openrouter_uses_the_shared_timeout(self):
        with patch('ai.client.requests.post') as post:
            post.return_value.json.return_value = {'choices': [{'message': {'content': 'hi'}}]}
            client._call_openrouter([{'role': 'user', 'content': 'x'}])

        self.assertEqual(post.call_args.kwargs['timeout'], 25)

    def test_fallback_order_is_unchanged_by_the_bounding(self):
        """Bounding must not alter which provider answers - a Groq timeout
        still falls through to Gemini rather than failing the request."""
        with patch('ai.client._call_groq', side_effect=RuntimeError('timed out')), \
             patch('ai.client._call_gemini', return_value='from gemini') as gemini:
            result = client._call_with_fallback([{'role': 'user', 'content': 'x'}])

        self.assertEqual(result, 'from gemini')
        gemini.assert_called_once()

    def test_all_providers_failing_still_raises(self):
        with patch('ai.client._call_groq', side_effect=RuntimeError('a')), \
             patch('ai.client._call_gemini', side_effect=RuntimeError('b')), \
             patch('ai.client._call_openrouter', side_effect=RuntimeError('c')):
            with self.assertRaises(RuntimeError):
                client._call_with_fallback([{'role': 'user', 'content': 'x'}])
