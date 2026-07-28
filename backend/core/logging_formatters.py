"""A Formatter that surfaces the `extra={...}` fields passed to logger calls
(logger.info('event', extra={'user_id': 5}) etc.) as a trailing JSON blob.

Without this, Python's default Formatter silently drops anything passed via
`extra` - it's stored on the LogRecord but never rendered unless a formatter
explicitly asks for it. Every OAuth/webhook/Celery/GitHub-API log call across
github_integration relies on this to actually be "structured logging" rather
than plain unstructured text with wasted context.
"""
import json
import logging

_STANDARD_RECORD_KEYS = frozenset(vars(logging.LogRecord('', 0, '', 0, '', (), None)).keys()) | {'message', 'asctime'}


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = {key: value for key, value in vars(record).items() if key not in _STANDARD_RECORD_KEYS}
        if not extra:
            return base
        try:
            return f'{base} {json.dumps(extra, default=str)}'
        except TypeError:
            return base
