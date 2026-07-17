from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import Analysis

from .client import generate_chat_reply
from .serializers import ChatRequestSerializer

BASE_SYSTEM_INSTRUCTION = (
    'You are a helpful AI assistant for a code analysis tool. Answer clearly and concisely, '
    'formatting code with markdown fences when useful.'
)


class ChatView(APIView):
    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        system_instruction = BASE_SYSTEM_INSTRUCTION
        analysis_id = data.get('analysis_id')
        if analysis_id is not None:
            analysis = get_object_or_404(Analysis, pk=analysis_id, owner=request.user)
            system_instruction += (
                f'\n\nThe user is asking about this analysis:\n'
                f'Name: {analysis.name}\nLanguage: {analysis.language}\n'
                f'Quality score: {analysis.quality_score}\nIssues: {analysis.issues}\n'
                f'Source code:\n{analysis.source_code}'
            )

        try:
            reply = generate_chat_reply(data['message'], data['history'], system_instruction)
        except Exception:
            return Response(
                {'detail': 'AI service is currently unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({'reply': reply})
