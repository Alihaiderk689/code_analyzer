"""Validates/bounds AI-generated content before the app trusts it enough to
persist it or use it further - the model is an external, potentially
adversarial-influenced text generator, not a validated data source, and its
output should be held to the same "verify before use" standard as any other
untrusted input, not given a free pass just because it originated from a
provider response rather than a request body.

Two shapes are validated differently on purpose:
- prose (suggestions, explanations, remediations, chat replies) is safe to
  truncate - a cut-off sentence is a cosmetic problem, not a correctness one.
- code (a refactor's rewritten source) is NOT safe to truncate - silently
  cutting off code would return something that looks like a valid result but
  may not even parse, which is worse than falling back to "couldn't parse a
  refactor" the caller already handles. Only rejected wholesale if it isn't
  a usable string at all, never trimmed.
"""
from __future__ import annotations

# Generous enough that no realistic explanation/suggestion/chat-reply/
# remediation ever gets truncated in normal operation (several pages of
# text) - this is a ceiling against a pathological/malformed provider
# response, not a tuned limit on typical output. Existing valid responses
# are all comfortably below it.
MAX_AI_PROSE_LENGTH = 20_000
# Matches AnalyzeRequestSerializer's own pasted-source-code cap - generous
# for a real refactor, still a hard ceiling.
MAX_AI_CODE_LENGTH = 200_000


def clean_ai_prose(value, max_length=MAX_AI_PROSE_LENGTH):
    """Returns a stripped, length-capped string, or None if `value` isn't a
    usable non-empty string. Callers decide the fallback for None - never
    pass a non-string/empty value through silently."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > max_length:
        value = value[:max_length].rstrip() + '…'
    return value


def is_valid_ai_code(value, max_length=MAX_AI_CODE_LENGTH):
    """True only if `value` is a non-empty string within the size ceiling -
    never truncates (see module docstring), so this is a pure accept/reject
    check, not a normalizer."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_length
