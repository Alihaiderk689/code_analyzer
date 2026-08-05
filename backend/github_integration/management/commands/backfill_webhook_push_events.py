"""One-off backfill for repositories that were selected before WEBHOOK_EVENTS
(see github_client.py) grew a 'push' entry - GitHub doesn't retroactively
change which events an already-existing webhook sends, so this PATCHes each
monitored repo's webhook directly via the GitHub API instead of requiring the
user to deselect/reselect the repo to get a fresh one. Safe to re-run:
PATCHing a webhook to the event list it already has is a no-op on GitHub's
side.

Run manually, once, after deploying the push-webhook support:
    python manage.py backfill_webhook_push_events
"""
from django.core.management.base import BaseCommand

from github_integration.models import GitHubRepository
from github_integration.services.github_client import GitHubAPIError, GitHubClient, WEBHOOK_EVENTS


class Command(BaseCommand):
    help = (
        "Updates every monitored repository's existing GitHub webhook to also send push events, "
        'so the dependency-graph index stays fresh after direct pushes to the default branch.'
    )

    def handle(self, *args, **options):
        repositories = (
            GitHubRepository.objects.filter(is_active=True, webhook_id__isnull=False)
            .select_related('integration')
        )

        updated = 0
        failed = 0
        for repository in repositories:
            owner, _, repo = repository.full_name.partition('/')
            try:
                GitHubClient(repository.integration.get_access_token()).update_webhook_events(
                    owner, repo, repository.webhook_id, WEBHOOK_EVENTS,
                )
            except GitHubAPIError as exc:
                failed += 1
                self.stderr.write(self.style.WARNING(f'{repository.full_name}: failed to update webhook ({exc})'))
                continue

            updated += 1
            self.stdout.write(self.style.SUCCESS(f'{repository.full_name}: webhook updated'))

        self.stdout.write(f'Done. {updated} updated, {failed} failed.')
