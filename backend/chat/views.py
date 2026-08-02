from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.client import generate_chat_reply
from ai.prompts import BASE_CHAT_INSTRUCTION, build_analysis_context
from analyses.models import Analysis
from core.throttling import AIRateThrottle

from .models import ChatMessage, Conversation
from .rate_limit import get_rate_limit_status
from .serializers import ChatMessageSerializer, ConversationSerializer, SendMessageSerializer

# How many previous turns get sent to the LLM as context. Bounds prompt size/cost
# as a conversation grows long; the database and the history endpoint always keep
# (and return) the full, untruncated conversation regardless of this limit.
HISTORY_LIMIT = 20


class StartConversationView(APIView):
    """Returns the existing conversation for this analysis, creating one on first use."""

    def post(self, request, analysis_id):
        analysis = get_object_or_404(Analysis, pk=analysis_id, owner=request.user)
        conversation, _created = Conversation.objects.get_or_create(analysis=analysis)
        return Response(ConversationSerializer(conversation).data)


class RateLimitStatusView(APIView):
    """Lets the frontend show 'X chats left' / a reset countdown without having
    to attempt (and get rejected on) a real message first."""

    def get(self, request):
        tz_offset_minutes = request.query_params.get('tz_offset_minutes', 0)
        return Response(get_rate_limit_status(request.user, tz_offset_minutes))


class SendMessageView(APIView):
    throttle_classes = [AIRateThrottle]

    def post(self, request):
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        limit_status = get_rate_limit_status(request.user, data.get('tz_offset_minutes', 0))
        if limit_status['remaining'] <= 0:
            return Response(
                {'detail': "You've used today's chat messages. Try again after the reset.", **limit_status},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        conversation = get_object_or_404(
            Conversation.objects.select_related('analysis'),
            pk=data['conversation_id'], analysis__owner=request.user,
        )
        analysis = conversation.analysis

        # Saved immediately, before calling the LLM, so the question is never lost
        # even if the AI call below fails.
        user_message = ChatMessage.objects.create(
            conversation=conversation, role=ChatMessage.Role.USER, message=data['message'],
        )

        recent = list(
            conversation.messages.exclude(pk=user_message.pk).order_by('-created_at')[:HISTORY_LIMIT]
        )
        history = [{'role': m.role, 'content': m.message} for m in reversed(recent)]
        system_instruction = BASE_CHAT_INSTRUCTION + build_analysis_context(analysis)

        try:
            reply = generate_chat_reply(data['message'], history, system_instruction)
        except Exception:
            return Response(
                {'detail': 'AI service is currently unavailable. Your message was saved - try again shortly.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ChatMessage.objects.create(conversation=conversation, role=ChatMessage.Role.ASSISTANT, message=reply)
        return Response({'reply': reply})


class ChatHistoryView(APIView):
    def get(self, request, conversation_id):
        conversation = get_object_or_404(Conversation, pk=conversation_id, analysis__owner=request.user)
        return Response(ChatMessageSerializer(conversation.messages.all(), many=True).data)

    def delete(self, request, conversation_id):
        """Clears this conversation's messages (the "Delete chat" button). The
        Conversation row itself is kept - not deleted - so its id stays valid and
        the next message sent doesn't need a fresh call to StartConversationView."""
        conversation = get_object_or_404(Conversation, pk=conversation_id, analysis__owner=request.user)
        deleted_count, _ = conversation.messages.all().delete()
        return Response({'detail': f'Deleted {deleted_count} message(s).', 'deleted_count': deleted_count})
