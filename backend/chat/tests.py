from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from analyses.models import Analysis

from .models import ChatMessage, Conversation
from .rate_limit import DAILY_MESSAGE_LIMIT, get_rate_limit_status

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


class RateLimitStatusTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()
        self.analysis = make_analysis(self.user)
        self.conversation = Conversation.objects.create(analysis=self.analysis)

    def test_requires_authentication(self):
        anon = APIClient()
        response = anon.get(reverse('chat-limit'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fresh_user_has_full_quota(self):
        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['limit'], DAILY_MESSAGE_LIMIT)
        self.assertEqual(response.data['used'], 0)
        self.assertEqual(response.data['remaining'], DAILY_MESSAGE_LIMIT)
        self.assertIsNone(response.data['reset_at'])

    def test_reflects_messages_sent_today(self):
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='q1')
        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.data['used'], 1)
        self.assertEqual(response.data['remaining'], DAILY_MESSAGE_LIMIT - 1)
        self.assertIsNone(response.data['reset_at'])

    def test_exhausted_quota_sets_reset_at(self):
        for _ in range(DAILY_MESSAGE_LIMIT):
            ChatMessage.objects.create(conversation=self.conversation, role='user', message='q')

        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.data['remaining'], 0)
        self.assertIsNotNone(response.data['reset_at'])

    def test_messages_older_than_24h_do_not_count(self):
        old = ChatMessage.objects.create(conversation=self.conversation, role='user', message='old')
        ChatMessage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))

        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.data['used'], 0)
        self.assertEqual(response.data['remaining'], DAILY_MESSAGE_LIMIT)

    def test_assistant_messages_do_not_count_against_quota(self):
        ChatMessage.objects.create(conversation=self.conversation, role='assistant', message='a reply')
        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.data['used'], 0)

    def test_quota_is_global_across_conversations(self):
        other_analysis = make_analysis(self.user, name='other.py')
        other_conversation = Conversation.objects.create(analysis=other_analysis)
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='q1')
        ChatMessage.objects.create(conversation=other_conversation, role='user', message='q2')

        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.data['used'], 2)

    def test_quota_scoped_to_owner(self):
        other_client, other_user = make_authenticated_client('other-quota@example.com')
        other_analysis = make_analysis(other_user, name='theirs.py')
        other_conversation = Conversation.objects.create(analysis=other_analysis)
        for _ in range(DAILY_MESSAGE_LIMIT):
            ChatMessage.objects.create(conversation=other_conversation, role='user', message='q')

        response = self.client.get(reverse('chat-limit'))
        self.assertEqual(response.data['used'], 0)
        self.assertEqual(response.data['remaining'], DAILY_MESSAGE_LIMIT)


class SendMessageRateLimitTests(APITestCase):
    def setUp(self):
        self.client, self.user = make_authenticated_client()
        self.analysis = make_analysis(self.user)
        self.conversation = Conversation.objects.create(analysis=self.analysis)

    @patch('chat.views.generate_chat_reply', return_value='ok')
    def test_allows_up_to_the_daily_limit(self, _mock_generate):
        for i in range(DAILY_MESSAGE_LIMIT):
            response = self.client.post(reverse('chat-message'), {
                'conversation_id': self.conversation.id, 'message': f'question {i}',
            })
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch('chat.views.generate_chat_reply', return_value='ok')
    def test_blocks_the_message_after_the_limit(self, mock_generate):
        for i in range(DAILY_MESSAGE_LIMIT):
            self.client.post(reverse('chat-message'), {
                'conversation_id': self.conversation.id, 'message': f'question {i}',
            })
        mock_generate.reset_mock()

        response = self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'one too many',
        })

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data['remaining'], 0)
        self.assertIsNotNone(response.data['reset_at'])
        # Blocked before ever reaching the LLM, and nothing extra was saved.
        mock_generate.assert_not_called()
        self.assertEqual(
            ChatMessage.objects.filter(conversation=self.conversation, role='user').count(),
            DAILY_MESSAGE_LIMIT,
        )

    @patch('chat.views.generate_chat_reply', return_value='ok')
    def test_limit_is_global_across_the_users_conversations(self, _mock_generate):
        other_analysis = make_analysis(self.user, name='other.py')
        other_conversation = Conversation.objects.create(analysis=other_analysis)

        self.client.post(reverse('chat-message'), {'conversation_id': self.conversation.id, 'message': 'q1'})
        self.client.post(reverse('chat-message'), {'conversation_id': other_conversation.id, 'message': 'q2'})
        self.client.post(reverse('chat-message'), {'conversation_id': self.conversation.id, 'message': 'q3'})

        response = self.client.post(reverse('chat-message'), {
            'conversation_id': other_conversation.id, 'message': 'q4 should be blocked',
        })
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch('chat.views.generate_chat_reply', return_value='ok')
    def test_access_restored_once_the_window_passes(self, _mock_generate):
        for i in range(DAILY_MESSAGE_LIMIT):
            self.client.post(reverse('chat-message'), {
                'conversation_id': self.conversation.id, 'message': f'question {i}',
            })
        ChatMessage.objects.filter(conversation=self.conversation, role='user').update(
            created_at=timezone.now() - timedelta(hours=25),
        )

        response = self.client.post(reverse('chat-message'), {
            'conversation_id': self.conversation.id, 'message': 'fresh start',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class RateLimitMidnightBoundaryTests(APITestCase):
    """Direct unit tests against get_rate_limit_status with a controlled `now` -
    day-boundary logic is exactly the kind of thing that's flaky to test by
    guessing what 'today' happens to be when the suite runs."""

    def setUp(self):
        self.user = User.objects.create_user(username='midnight', email='midnight@example.com', password='TestPass123!')
        self.analysis = make_analysis(self.user)
        self.conversation = Conversation.objects.create(analysis=self.analysis)

    def test_message_just_before_local_midnight_does_not_count_after_it(self):
        # UTC+5 (offset -300). "Now" is 00:05 local on Jan 2nd. A message sent
        # at 23:55 local on Jan 1st - only 10 minutes earlier in real time - is
        # still "yesterday" locally and must not count toward today's quota.
        now = timezone.datetime(2026, 1, 1, 19, 5, tzinfo=timezone.utc)
        msg = ChatMessage.objects.create(conversation=self.conversation, role='user', message='late night q')
        ChatMessage.objects.filter(pk=msg.pk).update(created_at=timezone.datetime(2026, 1, 1, 18, 55, tzinfo=timezone.utc))

        result = get_rate_limit_status(self.user, tz_offset_minutes=-300, now=now)
        self.assertEqual(result['used'], 0)

    def test_message_just_after_local_midnight_counts(self):
        now = timezone.datetime(2026, 1, 1, 19, 5, tzinfo=timezone.utc)
        msg = ChatMessage.objects.create(conversation=self.conversation, role='user', message='right after midnight')
        ChatMessage.objects.filter(pk=msg.pk).update(created_at=timezone.datetime(2026, 1, 1, 19, 1, tzinfo=timezone.utc))

        result = get_rate_limit_status(self.user, tz_offset_minutes=-300, now=now)
        self.assertEqual(result['used'], 1)

    def test_same_instant_different_reported_timezones_see_different_days(self):
        # Same real message, same real "now" - but a client reporting a
        # timezone that's further behind UTC hasn't rolled into the new day
        # yet, so yesterday's-by-UTC message is still "today" for them.
        now = timezone.datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)  # just after UTC midnight
        msg = ChatMessage.objects.create(conversation=self.conversation, role='user', message='q')
        ChatMessage.objects.filter(pk=msg.pk).update(created_at=timezone.datetime(2025, 12, 31, 23, 50, tzinfo=timezone.utc))

        utc_result = get_rate_limit_status(self.user, tz_offset_minutes=0, now=now)
        self.assertEqual(utc_result['used'], 0)  # message was "yesterday" (Dec 31) in UTC

        utc_minus_5_result = get_rate_limit_status(self.user, tz_offset_minutes=300, now=now)
        self.assertEqual(utc_minus_5_result['used'], 1)  # local time is only 19:10 Dec 31 - still today

    def test_reset_at_is_next_local_midnight_not_24h_from_last_message(self):
        # Matches the exact scenario this was designed around: using the last
        # of 3 messages at 9pm local should count down to midnight (~3h away),
        # not a full 24h from 9pm.
        now = timezone.datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)  # 21:00 local (UTC+5)
        for _ in range(DAILY_MESSAGE_LIMIT):
            msg = ChatMessage.objects.create(conversation=self.conversation, role='user', message='q')
            ChatMessage.objects.filter(pk=msg.pk).update(created_at=now)

        result = get_rate_limit_status(self.user, tz_offset_minutes=-300, now=now)
        self.assertEqual(result['reset_at'], timezone.datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc))

    def test_offset_is_clamped_to_real_world_range(self):
        now = timezone.now()
        ChatMessage.objects.create(conversation=self.conversation, role='user', message='q')

        wild = get_rate_limit_status(self.user, tz_offset_minutes=999_999, now=now)
        clamped_equivalent = get_rate_limit_status(self.user, tz_offset_minutes=12 * 60, now=now)
        self.assertEqual(wild['used'], clamped_equivalent['used'])

    def test_non_numeric_offset_falls_back_to_utc(self):
        now = timezone.now()
        result = get_rate_limit_status(self.user, tz_offset_minutes='not-a-number', now=now)
        utc_result = get_rate_limit_status(self.user, tz_offset_minutes=0, now=now)
        self.assertEqual(result, utc_result)
