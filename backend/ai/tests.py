from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

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
