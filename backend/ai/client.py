import logging

import requests
from django.conf import settings
from groq import Groq

from core.execution_budget import STAGE_AI_ENRICHMENT, BudgetExceeded

logger = logging.getLogger(__name__)

# A provider call given less than this has no realistic chance of returning a
# usable completion; starting one anyway would burn what's left of the budget
# and still fail. The chain stops instead - reported as a budget exhaustion,
# never as a provider failure.
MIN_AI_SLICE_SECONDS = 8

_groq_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        if not settings.GROQ_API_KEY:
            raise RuntimeError('GROQ_API_KEY is not configured.')
        # Explicit timeout + no SDK-level retries. The SDK defaults (60s read,
        # 2 retries) meant a single unresponsive Groq could consume ~3 minutes
        # before the chain even reached Gemini - long past the point gunicorn
        # kills the worker. Retrying is the fallback chain's job, not the SDK's.
        _groq_client = Groq(
            api_key=settings.GROQ_API_KEY,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
    return _groq_client


def _call_groq(messages, timeout=None):
    # Per-request `timeout` overrides the client-level one for this call only;
    # None leaves the client's own AI_REQUEST_TIMEOUT_SECONDS in force, which
    # is what every non-budgeted caller gets.
    kwargs = {} if timeout is None else {'timeout': timeout}
    completion = _get_groq_client().chat.completions.create(
        model=settings.GROQ_MODEL, messages=messages, **kwargs,
    )
    return completion.choices[0].message.content


def _call_gemini(messages, timeout=None):
    if not settings.GEMINI_API_KEY:
        raise RuntimeError('GEMINI_API_KEY is not configured.')

    system_parts = [m['content'] for m in messages if m['role'] == 'system']
    contents = [
        {'role': 'model' if m['role'] == 'assistant' else 'user', 'parts': [{'text': m['content']}]}
        for m in messages if m['role'] != 'system'
    ]
    payload = {'contents': contents}
    if system_parts:
        payload['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system_parts)}]}

    url = f'https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent'
    response = requests.post(
        url,
        # Header, NOT `params={'key': ...}`. requests puts the query string in
        # the URL, and requests.HTTPError's message is built as
        # "<status> Client Error: <reason> for url: <full url>" - so with the
        # key in the query string, every non-2xx from Gemini wrote the key
        # into the fallback chain's `exc_info=True` warning below, i.e. into
        # application logs. Google supports x-goog-api-key as an equivalent
        # to ?key=, and headers never appear in the exception message.
        headers={'x-goog-api-key': settings.GEMINI_API_KEY},
        json=payload,
        timeout=settings.AI_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data['candidates'][0]['content']['parts'][0]['text']


def _call_openrouter(messages, timeout=None):
    if not settings.OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not configured.')

    response = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {settings.OPENROUTER_API_KEY}'},
        json={'model': settings.OPENROUTER_MODEL, 'messages': messages},
        timeout=settings.AI_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']


def _call_with_fallback(messages, budget=None):
    # Tried in order; each is skipped in favor of the next on any failure
    # (rate limit, outage, missing key, ...), not just "limit reached" -
    # whatever knocks a provider out should not knock out the feature.
    # Referenced by bare name (not a module-level tuple of bound functions)
    # so callers/tests can monkeypatch e.g. `ai.client._call_groq`.
    providers = (
        ('groq', _call_groq),
        ('gemini', _call_gemini),
        ('openrouter', _call_openrouter),
    )
    last_error = None
    for name, call in providers:
        # `budget` is an optional request-wide deadline (see
        # core/execution_budget.py) that only the repository-context path
        # passes. None - every other caller - leaves the order, the retries
        # and the timeouts exactly as they were.
        timeout = None
        if budget is not None:
            if not budget.can_afford(MIN_AI_SLICE_SECONDS, STAGE_AI_ENRICHMENT):
                # Out of time, not out of providers. Raised rather than
                # returning `last_error` so the caller cannot mistake a
                # deadline for "every provider is down" - the fallback order
                # itself is untouched, we simply stopped walking it.
                logger.warning(
                    'AI fallback chain stopped early: request budget exhausted.',
                    extra={'provider': name, 'remaining_seconds': round(budget.remaining(), 2)},
                )
                raise BudgetExceeded(
                    'Request budget exhausted before the AI fallback chain completed.'
                ) from last_error
            timeout = min(settings.AI_REQUEST_TIMEOUT_SECONDS, budget.remaining())
        try:
            return call(messages, timeout=timeout)
        except BudgetExceeded:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                'AI provider %s failed, falling back to next provider.', name,
                exc_info=True, extra={'provider': name},
            )
    raise last_error


def generate_text(prompt, system_instruction=None, budget=None):
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    messages.append({'role': 'user', 'content': prompt})
    return _call_with_fallback(messages, budget=budget)


def generate_chat_reply(message, history=None, system_instruction=None):
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    for turn in history or []:
        messages.append({'role': turn['role'], 'content': turn['content']})
    messages.append({'role': 'user', 'content': message})
    return _call_with_fallback(messages)
