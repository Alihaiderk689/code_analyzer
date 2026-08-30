import json

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.client import generate_text
from ai.concurrency import AICapacityExhausted, ai_concurrency_slot
from ai.prompts import UNTRUSTED_DATA_WARNING, wrap_untrusted
from ai.validation import clean_ai_prose, is_valid_ai_code
from core.throttling import AIRateThrottle

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


def _repo_context_block(analysis):
    """'' for pasted/uploaded code; for a GitHub-repo-file-backed Analysis
    (see github_integration.repository_views._create_analysis_for_file_check),
    a block describing related files so suggestions/explanation/refactor
    account for how the file is actually used elsewhere, not just its own
    contents in isolation."""
    if not analysis.repo_context:
        return ''
    return f'\n\n{wrap_untrusted("REPOSITORY CONTEXT", analysis.repo_context)}\n'


def _call_ai(prompt, system_instruction, user_id):
    """Shared by Suggestions/Explanation/Refactor - centralizes both the
    provider-failure handling (503) and the per-user/global concurrency
    guard (429, see ai.concurrency), so a single fix in one place covers all
    three views."""
    try:
        with ai_concurrency_slot(user_id):
            return generate_text(prompt, system_instruction), None
    except AICapacityExhausted as exc:
        return None, Response({'detail': str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    except Exception:
        return None, Response(
            {'detail': 'AI service is currently unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


_SUGGESTION_CATEGORIES = {'security', 'general'}


def _normalize_suggestions(raw):
    """Each suggestion is {"category": "security"|"general", "text": "..."} so
    the frontend can group security-related suggestions under their own
    heading, separate from general style/quality ones. Also upgrades older
    cached analyses, which stored suggestions as a flat list of strings before
    categories existed, on the fly - no data migration needed."""
    normalized = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get('text'), str):
            text = clean_ai_prose(item['text'])
            if text is None:
                continue
            category = item.get('category') if item.get('category') in _SUGGESTION_CATEGORIES else 'general'
            normalized.append({'category': category, 'text': text})
        elif isinstance(item, str):
            text = clean_ai_prose(item)
            if text is None:
                continue
            normalized.append({'category': 'general', 'text': text})
    return normalized


def _parse_suggestions(text):
    try:
        data = json.loads(_strip_code_fences(text))
        if not isinstance(data, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        # Model didn't follow the requested JSON shape - fall back to treating
        # each non-empty line as an uncategorized suggestion rather than
        # dropping the response entirely.
        data = [line.strip('- ').strip() for line in text.strip().splitlines() if line.strip()]
    return _normalize_suggestions(data)


class SuggestionsView(APIView):
    throttle_classes = [AIRateThrottle]

    def get(self, request, pk):
        analysis, error = _get_owned_completed_analysis(request, pk)
        if error:
            return error

        if analysis.ai_suggestions and not _wants_regenerate(request):
            return Response({'suggestions': _normalize_suggestions(analysis.ai_suggestions), 'cached': True})

        prompt = (
            f'Language: {analysis.language}\n\n'
            f'Static analysis found {analysis.issues_count} issue(s):\n{json.dumps(analysis.issues, indent=2)}\n\n'
            f'{wrap_untrusted("SOURCE CODE", analysis.source_code)}'
            f'{_repo_context_block(analysis)}'
        )
        system_instruction = (
            'You are a senior software engineer performing a code review. Given source code and a list of '
            'static-analysis issues, produce concise, concrete, actionable suggestions to improve code quality. '
            'Tag each suggestion with a category: "security" if it relates to a security concern (injection, '
            'secrets, authentication, unsafe input handling, etc.), or "general" for anything else (style, '
            'performance, readability, correctness). If related files from the rest of the repository are '
            'provided below, use them to judge how this file is actually used elsewhere (e.g. how a function '
            'is called, what a caller expects back) rather than judging the file in isolation. Respond with '
            'ONLY a JSON array of the shape [{"category": "security"|"general", "text": "<the suggestion>"}, ...], '
            f'no other text, no markdown fences. {UNTRUSTED_DATA_WARNING}'
        )
        text, error = _call_ai(prompt, system_instruction, request.user.id)
        if error:
            return error

        suggestions = _parse_suggestions(text)

        analysis.ai_suggestions = suggestions
        analysis.save(update_fields=['ai_suggestions', 'updated_at'])
        return Response({'suggestions': suggestions, 'cached': False})


class ExplanationView(APIView):
    throttle_classes = [AIRateThrottle]

    def get(self, request, pk):
        analysis, error = _get_owned_completed_analysis(request, pk)
        if error:
            return error

        if analysis.ai_explanation and not _wants_regenerate(request):
            return Response({'explanation': analysis.ai_explanation, 'cached': True})

        prompt = (
            f'Language: {analysis.language}\n\n{wrap_untrusted("SOURCE CODE", analysis.source_code)}'
            f'{_repo_context_block(analysis)}'
        )
        system_instruction = (
            'You are a senior software engineer. Explain in plain, clear language what the following code does, '
            'in 2-4 short paragraphs aimed at a developer unfamiliar with this code. If related files from the '
            'rest of the repository are provided below, use them to explain how this file fits into the wider '
            'codebase (what depends on it, what it depends on), not just what it does on its own. Respond with '
            f'plain text only. {UNTRUSTED_DATA_WARNING}'
        )
        text, error = _call_ai(prompt, system_instruction, request.user.id)
        if error:
            return error

        explanation = clean_ai_prose(text) or ''
        analysis.ai_explanation = explanation
        analysis.save(update_fields=['ai_explanation', 'updated_at'])
        return Response({'explanation': explanation, 'cached': False})


class RefactorView(APIView):
    throttle_classes = [AIRateThrottle]

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
            f'{wrap_untrusted("SOURCE CODE", analysis.source_code)}'
            f'{_repo_context_block(analysis)}'
        )
        system_instruction = (
            'You are a senior software engineer. Rewrite the following code applying best practices and fixing '
            'the listed issues, while preserving behavior. If related files from the rest of the repository are '
            'provided below, make sure the rewrite still satisfies how callers actually use this file (function '
            'signatures/return shapes other files depend on must keep working) - do not break callers just to '
            'improve style. Respond with ONLY a JSON object of the shape '
            '{"code": "<the refactored code, no markdown fences>", "changes": [{"summary": "<what changed, one '
            'sentence>", "benefit": "<why it\'s better, one sentence>"}, ...]}, one entry per distinct change you '
            f'made, no other text. {UNTRUSTED_DATA_WARNING}'
        )
        text, error = _call_ai(prompt, system_instruction, request.user.id)
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
    follow the format - including when `code` parsed but isn't actually a usable
    string (e.g. a malformed/adversarial response returning {"code": {...}} or
    {"code": 123}): silently `.save()`-ing a non-string into a TextField would
    otherwise raise uncaught at save time instead of taking this already-handled
    fallback path. Never truncates `code` - see ai.validation's module docstring
    for why silently cutting off code is worse than falling back."""
    try:
        data = json.loads(_strip_code_fences(text))
        code = data['code']
        if not is_valid_ai_code(code):
            raise TypeError('refactor response "code" field was not a usable string')
        changes = data.get('changes', [])
        if not isinstance(changes, list):
            changes = []
    except (json.JSONDecodeError, KeyError, TypeError):
        code = _strip_code_fences(text)
        changes = []
    return code, changes
