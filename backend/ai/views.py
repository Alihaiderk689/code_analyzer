from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import Analysis
from core.throttling import AIRateThrottle

from .client import generate_chat_reply
from .prompts import BASE_CHAT_INSTRUCTION, build_analysis_context
from .serializers import ChatRequestSerializer


class ChatView(APIView):
    throttle_classes = [AIRateThrottle]

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        system_instruction = BASE_CHAT_INSTRUCTION
        analysis_id = data.get('analysis_id')
        if analysis_id is not None:
            analysis = get_object_or_404(Analysis, pk=analysis_id, owner=request.user)
            system_instruction += build_analysis_context(analysis)

        try:
            reply = generate_chat_reply(data['message'], data['history'], system_instruction)
        except Exception:
            return Response(
                {'detail': 'AI service is currently unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({'reply': reply})
