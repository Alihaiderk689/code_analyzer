"""Thin wrapper around Brevo's transactional email API using `requests`
(already a project dependency) rather than Brevo's official SDK - the surface
area this needs (send one transactional email) is a single JSON endpoint, and
a bespoke SDK dependency isn't worth it for that (same reasoning as
github_integration/services/github_client.py's own docstring).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

BREVO_API_BASE = 'https://api.brevo.com/v3/'
REQUEST_TIMEOUT_SECONDS = 15


class BrevoAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class BrevoClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else settings.BREVO_API_KEY
        if not self.api_key:
            raise ImproperlyConfigured(
                'BREVO_API_KEY is not configured - create one at '
                'https://app.brevo.com/settings/keys/api and set it in .env.'
            )

    def _headers(self) -> dict:
        return {
            'api-key': self.api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def send_email(
        self, to_email: str, subject: str, html_content: str, text_content: Optional[str] = None,
    ) -> dict:
        if not settings.BREVO_SENDER_EMAIL:
            raise ImproperlyConfigured('BREVO_SENDER_EMAIL is not configured - set it in .env.')

        payload = {
            'sender': {'name': settings.BREVO_SENDER_NAME, 'email': settings.BREVO_SENDER_EMAIL},
            'to': [{'email': to_email}],
            'subject': subject,
            'htmlContent': html_content,
        }
        if text_content:
            payload['textContent'] = text_content

        url = f'{BREVO_API_BASE}smtp/email'
        try:
            response = requests.post(
                url, headers=self._headers(), json=payload, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.error('brevo_api.network_error', extra={'url': url, 'error': str(exc)})
            raise BrevoAPIError(f'Network error calling Brevo API: {exc}') from exc

        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            logger.warning(
                'brevo_api.error_response',
                extra={'url': url, 'status_code': response.status_code, 'body': body},
            )
            raise BrevoAPIError(
                f'Brevo API request failed ({response.status_code})', status_code=response.status_code,
                response_body=body,
            )

        return response.json()
