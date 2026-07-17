from django.conf import settings
from groq import Groq

_client = None


def _get_client():
    global _client
    if _client is None:
        if not settings.GROQ_API_KEY:
            raise RuntimeError('GROQ_API_KEY is not configured.')
        _client = Groq(api_key=settings.GROQ_API_KEY)
    return _client


def generate_text(prompt, system_instruction=None):
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    messages.append({'role': 'user', 'content': prompt})
    completion = _get_client().chat.completions.create(model=settings.GROQ_MODEL, messages=messages)
    return completion.choices[0].message.content


def generate_chat_reply(message, history=None, system_instruction=None):
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    for turn in history or []:
        messages.append({'role': turn['role'], 'content': turn['content']})
    messages.append({'role': 'user', 'content': message})
    completion = _get_client().chat.completions.create(model=settings.GROQ_MODEL, messages=messages)
    return completion.choices[0].message.content
