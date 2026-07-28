import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services.webhook_service import WebhookService, WebhookVerificationError
from .tasks import process_pull_request_webhook

logger = logging.getLogger(__name__)


class GitHubWebhookView(APIView):
    """POST /api/webhooks/github/ - called by GitHub itself, not our frontend:
    no JWT is possible here, so authenticity is proven instead by the
    X-Hub-Signature-256 HMAC header, verified against GITHUB_WEBHOOK_SECRET.

    Deliberately reads request.body directly (raw bytes) rather than
    request.data - the signature is computed over the exact raw payload
    GitHub sent, and re-serializing parsed JSON would not reliably reproduce
    those exact bytes.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        event_type = request.headers.get('X-GitHub-Event', '')
        delivery_id = request.headers.get('X-GitHub-Delivery', '')
        signature = request.headers.get('X-Hub-Signature-256', '')

        if not event_type or not delivery_id:
            return Response(
                {'detail': 'Missing required GitHub webhook headers.'}, status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            event, should_process = WebhookService().receive(
                payload_body=request.body,
                signature_header=signature,
                event_type=event_type,
                delivery_id=delivery_id,
                secret=settings.GITHUB_WEBHOOK_SECRET,
            )
        except WebhookVerificationError:
            logger.warning('github_webhook.invalid_signature', extra={'delivery_id': delivery_id})
            return Response({'detail': 'Invalid signature.'}, status=status.HTTP_401_UNAUTHORIZED)

        if should_process:
            process_pull_request_webhook.delay(event.id)

        # 202: the delivery is accepted; GitHub doesn't wait for (and will
        # time out and retry if we make it wait for) the actual analysis to
        # finish - that's the entire reason this hands off to Celery.
        return Response({'detail': 'Accepted.'}, status=status.HTTP_202_ACCEPTED)
