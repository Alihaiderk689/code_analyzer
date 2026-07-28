"""Receives a raw GitHub webhook delivery: verifies its signature, persists it
as a WebhookEvent (the transport-level record of *every* delivery, whether or
not we act on it - useful for debugging/auditing regardless), and decides
whether it's interesting enough to queue a Celery task for.
"""
from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction

from ..models import GitHubRepository, WebhookEvent
from .signature import verify_signature

logger = logging.getLogger(__name__)

# Only these pull_request actions change the code being reviewed - labeled,
# assigned, closed, etc. don't need a fresh analysis.
HANDLED_PR_ACTIONS = {'opened', 'reopened', 'synchronize', 'edited'}


class WebhookVerificationError(Exception):
    """Signature missing or invalid - the request never touches the DB."""


class WebhookService:
    def receive(
        self, *, payload_body: bytes, signature_header: str, event_type: str, delivery_id: str, secret: str,
    ) -> tuple[WebhookEvent, bool]:
        """Returns (event, should_process). should_process is False for
        duplicate deliveries (an already-seen delivery_id) and for events/
        actions this app doesn't act on - the view still responds 200/202
        either way, since "not interesting" isn't a failure."""
        if not verify_signature(payload_body, signature_header, secret):
            raise WebhookVerificationError('Invalid webhook signature.')

        payload = json.loads(payload_body)

        try:
            # Nested atomic() creates a savepoint: if the create() hits the
            # unique constraint, only this savepoint rolls back rather than
            # poisoning whatever outer transaction this is called within
            # (tests wrap each test in one, and callers may too).
            with transaction.atomic():
                event = WebhookEvent.objects.create(event_type=event_type, delivery_id=delivery_id, payload=payload)
        except IntegrityError:
            logger.info('github_webhook.duplicate_delivery', extra={'delivery_id': delivery_id})
            return WebhookEvent.objects.get(delivery_id=delivery_id), False

        should_process = self._should_process(event_type, payload)
        logger.info(
            'github_webhook.received',
            extra={'event_type': event_type, 'delivery_id': delivery_id, 'should_process': should_process},
        )
        return event, should_process

    @staticmethod
    def _should_process(event_type: str, payload: dict) -> bool:
        if event_type != 'pull_request':
            return False
        if payload.get('action') not in HANDLED_PR_ACTIONS:
            return False
        repository_id = payload.get('repository', {}).get('id')
        return GitHubRepository.objects.filter(repository_id=repository_id, is_active=True).exists()
