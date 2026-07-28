import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from ..models import WebhookEvent
from ..services.webhook_service import WebhookService, WebhookVerificationError
from .factories import make_integration, make_repository, make_user, pull_request_webhook_payload as _pr_payload

WEBHOOK_SECRET = 'test-webhook-secret'


def _signed_body(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    signature = 'sha256=' + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


class WebhookServiceTests(TestCase):
    def test_invalid_signature_raises_verification_error(self):
        body = json.dumps(_pr_payload()).encode()
        with self.assertRaises(WebhookVerificationError):
            WebhookService().receive(
                payload_body=body, signature_header='sha256=wrong', event_type='pull_request',
                delivery_id='d1', secret=WEBHOOK_SECRET,
            )
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_valid_signature_persists_event(self):
        body, signature = _signed_body(_pr_payload())
        event, _should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='d1', secret=WEBHOOK_SECRET,
        )
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(event.delivery_id, 'd1')

    def test_duplicate_delivery_id_does_not_create_second_row_and_returns_should_process_false(self):
        body, signature = _signed_body(_pr_payload())
        WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='dup', secret=WEBHOOK_SECRET,
        )
        event, should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='dup', secret=WEBHOOK_SECRET,
        )
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertFalse(should_process)
        self.assertEqual(event.delivery_id, 'dup')

    def test_a_third_delivery_after_a_duplicate_still_works(self):
        # Regression guard for the IntegrityError-inside-atomic-block bug: the
        # savepoint must be scoped to just the failed create(), not poison
        # every later query in the same request/test transaction.
        body, signature = _signed_body(_pr_payload())
        WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='dup', secret=WEBHOOK_SECRET,
        )
        WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='dup', secret=WEBHOOK_SECRET,
        )
        make_user()  # any ordinary query must still work after the caught IntegrityError
        self.assertEqual(WebhookEvent.objects.count(), 1)

    def test_non_pull_request_event_should_not_process(self):
        body, signature = _signed_body({'zen': 'Keep it logically awesome.'})
        _event, should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='ping',
            delivery_id='d1', secret=WEBHOOK_SECRET,
        )
        self.assertFalse(should_process)

    def test_unhandled_pr_action_should_not_process(self):
        body, signature = _signed_body(_pr_payload(action='labeled'))
        _event, should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='d1', secret=WEBHOOK_SECRET,
        )
        self.assertFalse(should_process)

    def test_pr_for_unmonitored_repository_should_not_process(self):
        body, signature = _signed_body(_pr_payload(repository_id=999999))
        _event, should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='d1', secret=WEBHOOK_SECRET,
        )
        self.assertFalse(should_process)

    def test_pr_opened_for_monitored_repository_should_process(self):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        body, signature = _signed_body(_pr_payload(action='opened', repository_id=2001))

        _event, should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='d1', secret=WEBHOOK_SECRET,
        )
        self.assertTrue(should_process)

    def test_pr_for_inactive_repository_should_not_process(self):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=False)
        body, signature = _signed_body(_pr_payload(action='opened', repository_id=2001))

        _event, should_process = WebhookService().receive(
            payload_body=body, signature_header=signature, event_type='pull_request',
            delivery_id='d1', secret=WEBHOOK_SECRET,
        )
        self.assertFalse(should_process)


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class GitHubWebhookViewTests(TestCase):
    def _post(self, payload, event_type='pull_request', delivery_id='d1', signature=None):
        body, real_signature = _signed_body(payload)
        return APIClient().post(
            reverse('github-webhook'), data=body, content_type='application/json',
            HTTP_X_GITHUB_EVENT=event_type, HTTP_X_GITHUB_DELIVERY=delivery_id,
            HTTP_X_HUB_SIGNATURE_256=signature or real_signature,
        )

    def test_missing_headers_returns_400(self):
        response = APIClient().post(reverse('github-webhook'), data=b'{}', content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_signature_returns_401(self):
        response = self._post(_pr_payload(), signature='sha256=deadbeef')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('github_integration.webhook_views.process_pull_request_webhook')
    def test_monitored_pr_event_queues_task_and_returns_202(self, mock_task):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)

        response = self._post(_pr_payload(repository_id=2001))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task.delay.assert_called_once()
        event_id_arg = mock_task.delay.call_args.args[0]
        self.assertEqual(WebhookEvent.objects.get(pk=event_id_arg).delivery_id, 'd1')

    @patch('github_integration.webhook_views.process_pull_request_webhook')
    def test_uninteresting_event_still_returns_202_but_does_not_queue_task(self, mock_task):
        response = self._post({'zen': 'hello'}, event_type='ping')
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task.delay.assert_not_called()

    @patch('github_integration.webhook_views.process_pull_request_webhook')
    def test_duplicate_delivery_does_not_requeue(self, mock_task):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, is_active=True)
        payload = _pr_payload(repository_id=2001)

        self._post(payload, delivery_id='dup')
        response = self._post(payload, delivery_id='dup')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task.delay.assert_called_once()
