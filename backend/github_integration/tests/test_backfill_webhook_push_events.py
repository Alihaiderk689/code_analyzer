from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from ..services.github_client import GitHubAPIError, WEBHOOK_EVENTS
from .factories import make_integration, make_repository, make_user

_PATCHED_CLIENT = 'github_integration.management.commands.backfill_webhook_push_events.GitHubClient'


class BackfillWebhookPushEventsCommandTests(TestCase):
    @patch(_PATCHED_CLIENT)
    def test_updates_webhook_for_each_active_monitored_repository(self, mock_client_cls):
        integration = make_integration(make_user())
        repository = make_repository(
            integration, repository_id=2001, full_name='octocat/hello-world', webhook_id=555, is_active=True,
        )

        out = StringIO()
        call_command('backfill_webhook_push_events', stdout=out)

        mock_client_cls.return_value.update_webhook_events.assert_called_once_with(
            'octocat', 'hello-world', repository.webhook_id, WEBHOOK_EVENTS,
        )
        self.assertIn('1 updated, 0 failed', out.getvalue())

    @patch(_PATCHED_CLIENT)
    def test_skips_inactive_and_webhook_less_repositories(self, mock_client_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, webhook_id=555, is_active=False)
        make_repository(integration, repository_id=2002, webhook_id=None, is_active=True)

        out = StringIO()
        call_command('backfill_webhook_push_events', stdout=out)

        mock_client_cls.return_value.update_webhook_events.assert_not_called()
        self.assertIn('0 updated, 0 failed', out.getvalue())

    @patch(_PATCHED_CLIENT)
    def test_continues_past_a_failure_and_reports_it(self, mock_client_cls):
        integration = make_integration(make_user())
        make_repository(integration, repository_id=2001, full_name='octocat/broken', webhook_id=555, is_active=True)
        make_repository(integration, repository_id=2002, full_name='octocat/fine', webhook_id=556, is_active=True)

        def _side_effect(_owner, _repo, hook_id, _events):
            if hook_id == 555:
                raise GitHubAPIError('down', 500)
            return {'id': hook_id}

        mock_client_cls.return_value.update_webhook_events.side_effect = _side_effect

        out = StringIO()
        err = StringIO()
        call_command('backfill_webhook_push_events', stdout=out, stderr=err)

        self.assertEqual(mock_client_cls.return_value.update_webhook_events.call_count, 2)
        self.assertIn('1 updated, 1 failed', out.getvalue())
        self.assertIn('octocat/broken', err.getvalue())
