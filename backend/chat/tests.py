from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from analyses.models import Analysis

from .models import ChatMessage, Conversation

User = get_user_model()


def make_authenticated_client(email='chatuser@example.com'):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password='TestPass123!')
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def make_analysis(owner, **overrides):
    defaults = dict(
        name='snippet.py', language='Python', source_code='def add(a, b):\n    return a + b\n',
        status=Analysis.Status.COMPLETED, quality_score=95.0, lines_of_code=2,
        issues=[{'type': 'unused_import', 'line': 1, 'message': "'os' imported but unused"}],
        issues_count=1,
    )
    defaults.update(overrides)
    return Analysis.objects.create(owner=owner, **defaults)


class StartConversationTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()
        self.analysis = make_analysis(self.user)

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.post(reverse('chat-start', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_creates_conversation_on_first_call(self):
        self.assertEqual(Conversation.objects.count(), 0)
        response = self.client.post(reverse('chat-start', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(response.data['analysis'], self.analysis.id)

    def test_returns_same_conversation_on_repeat_calls(self):
        first = self.client.post(reverse('chat-start', args=[self.analysis.id]))
        second = self.client.post(reverse('chat-start', args=[self.analysis.id]))
        self.assertEqual(first.data['id'], second.data['id'])
        self.assertEqual(Conversation.objects.count(), 1)

    def test_404_for_another_users_analysis(self):
        other_client, _other_user = make_authenticated_client('other@example.com')
        response = other_client.post(reverse('chat-start', args=[self.analysis.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_one_conversation_per_analysis_enforced_at_db_level(self):
        Conversation.objects.create(analysis=self.analysis)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Conversation.objects.create(analysis=self.analysis)


class SendMessageTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()
        self.analysis = make_analysis(self.user)
        self.conversation = Conversation.objects.create(analysis=self.analysis)

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'Why is issue #1 a problem?',
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_empty_message(self):
        response = self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': '   ',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_404_for_conversation_owned_by_another_user(self):
        other_client, _other_user = make_authenticated_client('intruder@example.com')
        response = other_client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'Show me the source.',
        })
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('chat.views.generate_chat_reply')
    def test_saves_both_messages_and_returns_reply(self, mock_generate):
        mock_generate.return_value = 'Because it wastes memory unnecessarily.'

        response = self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'Why is issue #1 a problem?',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['reply'], 'Because it wastes memory unnecessarily.')

        messages = list(self.conversation.messages.all())
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, 'user')
        self.assertEqual(messages[0].message, 'Why is issue #1 a problem?')
        self.assertEqual(messages[1].role, 'assistant')
        self.assertEqual(messages[1].message, 'Because it wastes memory unnecessarily.')

    @patch('chat.views.generate_chat_reply')
    def test_prompt_includes_source_code_and_numbered_issues(self, mock_generate):
        mock_generate.return_value = 'ok'
        self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'Explain this.',
        })

        _args, kwargs = mock_generate.call_args
        call_args = mock_generate.call_args.args
        system_instruction = call_args[2] if len(call_args) > 2 else kwargs.get('system_instruction')
        self.assertIn('def add(a, b):', system_instruction)
        self.assertIn("1. [unused_import] line 1: 'os' imported but unused", system_instruction)

    @patch('chat.views.generate_chat_reply')
    def test_prior_messages_sent_as_history_in_chronological_order(self, mock_generate):
        mock_generate.return_value = 'third reply'
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='first question')
        ChatMessage.objects.create(conversation=self.conversation, role='assistant', message='first answer')

        self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'second question',
        })

        call_args = mock_generate.call_args.args
        history = call_args[1]
        self.assertEqual(
            [(h['role'], h['content']) for h in history],
            [('user', 'first question'), ('assistant', 'first answer')],
        )
        self.assertEqual(call_args[0], 'second question')

    @patch('chat.views.generate_chat_reply', side_effect=RuntimeError('boom'))
    def test_ai_failure_returns_503_but_keeps_user_message(self, _mock_generate):
        response = self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'This will fail.',
        })

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        messages = list(self.conversation.messages.all())
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, 'user')
        self.assertEqual(messages[0].message, 'This will fail.')


class ChatHistoryTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()
        self.analysis = make_analysis(self.user)
        self.conversation = Conversation.objects.create(analysis=self.analysis)

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.get(reverse('chat-history', args=[self.conversation.id]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_messages_in_chronological_order(self):
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='q1')
        ChatMessage.objects.create(conversation=self.conversation, role='assistant', message='a1')
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='q2')

        response = self.client.get(reverse('chat-history', args=[self.conversation.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([m['message'] for m in response.data], ['q1', 'a1', 'q2'])

    def test_404_for_conversation_owned_by_another_user(self):
        other_client, _other_user = make_authenticated_client('another@example.com')
        response = other_client.get(reverse('chat-history', args=[self.conversation.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class DeleteChatHistoryTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()
        self.analysis = make_analysis(self.user)
        self.conversation = Conversation.objects.create(analysis=self.analysis)
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='q1')
        ChatMessage.objects.create(conversation=self.conversation, role='assistant', message='a1')

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.delete(reverse('chat-history', args=[self.conversation.id]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_clears_messages_but_keeps_conversation(self):
        response = self.client.delete(reverse('chat-history', args=[self.conversation.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['deleted_count'], 2)
        self.assertEqual(self.conversation.messages.count(), 0)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation.id).exists())

    def test_conversation_id_still_usable_after_clearing(self):
        self.client.delete(reverse('chat-history', args=[self.conversation.id]))
        with patch('chat.views.generate_chat_reply', return_value='fresh start'):
            response = self.client.post(reverse('chat-message'), {
                'conversation_id': self.conversation.id, 'message': 'starting over',
            })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.conversation.messages.count(), 2)

    def test_404_for_conversation_owned_by_another_user(self):
        other_client, _other_user = make_authenticated_client('yet-another@example.com')
        response = other_client.delete(reverse('chat-history', args=[self.conversation.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.conversation.messages.count(), 2)
