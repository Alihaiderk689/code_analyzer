import logging

import requests
from django.conf import settings
from groq import Groq

logger = logging.getLogger(__name__)

_groq_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        if not settings.GROQ_API_KEY:
            raise RuntimeError('GROQ_API_KEY is not configured.')
        _groq_client = Groq(api_key=settings.GROQ_API_KEY)
    return _groq_client


def _call_groq(messages):
    completion = _get_groq_client().chat.completions.create(model=settings.GROQ_MODEL, messages=messages)
    return completion.choices[0].message.content


def _call_gemini(messages):
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
    response = requests.post(url, params={'key': settings.GEMINI_API_KEY}, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data['candidates'][0]['content']['parts'][0]['text']


def _call_openrouter(messages):
    if not settings.OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not configured.')

    response = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {settings.OPENROUTER_API_KEY}'},
        json={'model': settings.OPENROUTER_MODEL, 'messages': messages},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']


def _call_with_fallback(messages):
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
        try:
            return call(messages)
        except Exception as exc:
            last_error = exc
            logger.warning(
                'AI provider %s failed, falling back to next provider.', name,
                exc_info=True, extra={'provider': name},
            )
    raise last_error


def generate_text(prompt, system_instruction=None):
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    messages.append({'role': 'user', 'content': prompt})
    return _call_with_fallback(messages)


def generate_chat_reply(message, history=None, system_instruction=None):
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    for turn in history or []:
        messages.append({'role': turn['role'], 'content': turn['content']})
    messages.append({'role': 'user', 'content': message})
    return _call_with_fallback(messages)
