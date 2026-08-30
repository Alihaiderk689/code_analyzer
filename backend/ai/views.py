from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import Analysis
from core.throttling import AIRateThrottle

from .client import generate_chat_reply
from .concurrency import AICapacityExhausted, ai_concurrency_slot
from .prompts import BASE_CHAT_INSTRUCTION, build_analysis_context
from .serializers import ChatRequestSerializer
from .validation import clean_ai_prose


class ChatView(APIView):
    throttle_classes = [AIRateThrottle]

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # system_instruction stays limited to trusted, server-authored text -
        # the analysis being discussed is untrusted, submitted content, so it
        # goes in `context` (a user-role message) instead, same trust tier as
        # every other prompt-building call site uses for untrusted data.
        context = None
        analysis_id = data.get('analysis_id')
        if analysis_id is not None:
            analysis = get_object_or_404(Analysis, pk=analysis_id, owner=request.user)
            context = build_analysis_context(analysis)

        try:
            with ai_concurrency_slot(request.user.id):
                reply = generate_chat_reply(data['message'], data['history'], BASE_CHAT_INSTRUCTION, context=context)
        except AICapacityExhausted as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except Exception:
            return Response(
                {'detail': 'AI service is currently unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        reply = clean_ai_prose(reply) or ''
        return Response({'reply': reply})
