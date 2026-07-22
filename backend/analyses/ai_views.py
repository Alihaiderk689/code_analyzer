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
            try:
                cached_changes = json.loads(analysis.ai_refactor_explanation or '[]')
            except json.JSONDecodeError:
                cached_changes = []
            return Response({
                'refactored_code': analysis.ai_refactored_code,
                'explanation': cached_changes,
                'cached': True,
            })

        prompt = (
            f'Language: {analysis.language}\n\n'
            f'Known issues:\n{json.dumps(analysis.issues, indent=2)}\n\n'
            f'Source code:\n{analysis.source_code}'
        )
        system_instruction = (
            'You are a senior software engineer. Rewrite the following code applying best practices and fixing '
            'the listed issues, while preserving behavior. Respond with ONLY a JSON object of the shape '
            '{"code": "<the refactored code, no markdown fences>", "changes": [{"summary": "<what changed, one '
            'sentence>", "benefit": "<why it\'s better, one sentence>"}, ...]}, one entry per distinct change you '
            'made, no other text.'
        )
        text, error = _call_ai(prompt, system_instruction)
        if error:
            return error

        refactored_code, changes = _parse_refactor_response(text)

        analysis.ai_refactored_code = refactored_code
        analysis.ai_refactor_explanation = json.dumps(changes)
        analysis.save(update_fields=['ai_refactored_code', 'ai_refactor_explanation', 'updated_at'])
        return Response({'refactored_code': refactored_code, 'explanation': changes, 'cached': False})


def _parse_refactor_response(text):
    """Parses the {"code", "changes"} JSON the refactor prompt asks for. Falls back to
    treating the whole response as raw code with no explanation if the model didn't
    follow the format."""
    try:
        data = json.loads(_strip_code_fences(text))
        code = data['code']
        changes = data.get('changes', [])
        if not isinstance(changes, list):
            changes = []
    except (json.JSONDecodeError, KeyError, TypeError):
        code = _strip_code_fences(text)
        changes = []
    return code, changes
