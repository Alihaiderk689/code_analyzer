import json

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.client import generate_text

from .models import Analysis


def _strip_code_fences(text):
    text = text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)
    return text.strip()


def _get_owned_completed_analysis(request, pk):
    analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
    if analysis.status != Analysis.Status.COMPLETED:
        return None, Response(
            {'detail': f'Analysis must be completed before requesting this (current status: "{analysis.status}").'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return analysis, None


def _wants_regenerate(request):
    return request.query_params.get('regenerate', '').lower() == 'true'


def _call_ai(prompt, system_instruction):
    try:
        return generate_text(prompt, system_instruction), None
    except Exception:
        return None, Response(
            {'detail': 'AI service is currently unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class SuggestionsView(APIView):
    def get(self, request, pk):
        analysis, error = _get_owned_completed_analysis(request, pk)
        if error:
            return error

        if analysis.ai_suggestions and not _wants_regenerate(request):
            return Response({'suggestions': analysis.ai_suggestions, 'cached': True})

        prompt = (
            f'Language: {analysis.language}\n\n'
            f'Static analysis found {analysis.issues_count} issue(s):\n{json.dumps(analysis.issues, indent=2)}\n\n'
            f'Source code:\n{analysis.source_code}'
        )
        system_instruction = (
            'You are a senior software engineer performing a code review. Given source code and a list of '
            'static-analysis issues, produce concise, concrete, actionable suggestions to improve code quality. '
            'Respond with ONLY a JSON array of strings, no other text, no markdown fences.'
        )
        text, error = _call_ai(prompt, system_instruction)
        if error:
            return error

        try:
            suggestions = json.loads(_strip_code_fences(text))
            if not isinstance(suggestions, list):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            suggestions = [line.strip('- ').strip() for line in text.strip().splitlines() if line.strip()]

        analysis.ai_suggestions = suggestions
        analysis.save(update_fields=['ai_suggestions', 'updated_at'])
        return Response({'suggestions': suggestions, 'cached': False})


class ExplanationView(APIView):
    def get(self, request, pk):
        analysis, error = _get_owned_completed_analysis(request, pk)
        if error:
            return error

        if analysis.ai_explanation and not _wants_regenerate(request):
            return Response({'explanation': analysis.ai_explanation, 'cached': True})

        prompt = f'Language: {analysis.language}\n\nSource code:\n{analysis.source_code}'
        system_instruction = (
            'You are a senior software engineer. Explain in plain, clear language what the following code does, '
            'in 2-4 short paragraphs aimed at a developer unfamiliar with this code. Respond with plain text only.'
        )
        text, error = _call_ai(prompt, system_instruction)
        if error:
            return error

        explanation = text.strip()
        analysis.ai_explanation = explanation
        analysis.save(update_fields=['ai_explanation', 'updated_at'])
        return Response({'explanation': explanation, 'cached': False})


class RefactorView(APIView):
    def get(self, request, pk):
        analysis, error = _get_owned_completed_analysis(request, pk)
        if error:
            return error

        if analysis.ai_refactored_code and not _wants_regenerate(request):
            return Response({'refactored_code': analysis.ai_refactored_code, 'cached': True})

        prompt = (
            f'Language: {analysis.language}\n\n'
            f'Known issues:\n{json.dumps(analysis.issues, indent=2)}\n\n'
            f'Source code:\n{analysis.source_code}'
        )
        system_instruction = (
            'You are a senior software engineer. Rewrite the following code applying best practices and fixing '
            'the listed issues, while preserving behavior. Respond with ONLY the refactored code, no explanation, '
            'no markdown fences.'
        )
        text, error = _call_ai(prompt, system_instruction)
        if error:
            return error

        refactored_code = _strip_code_fences(text)
        analysis.ai_refactored_code = refactored_code
        analysis.save(update_fields=['ai_refactored_code', 'updated_at'])
        return Response({'refactored_code': refactored_code, 'cached': False})
