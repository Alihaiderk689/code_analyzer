"""Redacts likely secrets from repository-derived content immediately before
it leaves this application in a request to a third-party AI provider.

Deliberately independent from `analyses.services.custom_rules_service`'s
scanning rules, despite overlapping patterns: that module's job is to
*report* findings to the user (an accuracy-focused surface - a false
positive there is a wrong/annoying issue shown to the user), while this
module's job is to prevent *egress* of anything that plausibly looks like a
secret (a recall-focused surface - a false positive here just means a
harmless string gets replaced with a placeholder before a prompt is sent,
which is a far cheaper mistake than a false negative letting a real secret
out to a third party). Keeping them separate means either can change for its
own reasons without silently weakening the other.

Applied centrally in `ai/client.py`, immediately before building the message
list for a provider call - not scattered across each of the five prompt-
building call sites - so nothing can reach a provider without passing
through it, regardless of which endpoint triggered the AI call.
"""
from __future__ import annotations

import re

REDACTED_PLACEHOLDER = '[REDACTED_SECRET]'

# Deliberately broad and pattern-based (recall over precision - see module
# docstring) rather than trying to enumerate every possible secret shape.
_SECRET_PATTERNS = (
    # `KEY = "value"` / `KEY: "value"` assignment style - the same shape a
    # committed secret in source code actually takes, independent of the
    # specific variable name used.
    re.compile(
        r'(?:SECRET_KEY|API_KEY|APIKEY|ACCESS_KEY|SECRET_ACCESS_KEY|PRIVATE_KEY|AUTH_TOKEN|'
        r'CLIENT_SECRET|PASSWORD|PASSWD|TOKEN|WEBHOOK_SECRET|ENCRYPTION_KEY)'
        r'\s*[:=]\s*[\'"][^\'"\n]{6,}[\'"]',
        re.IGNORECASE,
    ),
    # Common vendor token prefixes - matched regardless of surrounding syntax,
    # since these are distinctive enough on their own not to need one.
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),              # GitHub tokens
    re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'),             # Slack tokens
    re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'),                      # OpenAI/Stripe-style secret keys
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),                         # AWS access key id
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.DOTALL),
    # `Authorization: Bearer <token>` / a bare bearer token string.
    re.compile(r'\bBearer\s+[A-Za-z0-9\-_.=]{15,}\b'),
)


def redact_secrets(text):
    """Returns `text` with every match of a known secret shape replaced by a
    stable placeholder. Safe on non-string/falsy input - returns it
    unchanged rather than raising, since callers apply this uniformly to
    values that aren't always guaranteed to be strings (e.g. an already-None
    optional prompt argument)."""
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED_PLACEHOLDER, redacted)
    return redacted
