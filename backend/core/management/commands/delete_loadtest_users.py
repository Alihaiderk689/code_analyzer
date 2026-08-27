"""Tears down every load-test fixture user and, by cascade, their analyses.

Matches only the reserved namespace from _loadtest.py (username prefix
`loadtest_` AND an @loadtest.invalid email), so it cannot reach a real
account even if run against the wrong database. Requires --yes, because the
delete is not recoverable.
"""
from django.core.management.base import BaseCommand, CommandError

from ._loadtest import guard_environment, loadtest_users_queryset


class Command(BaseCommand):
    help = 'Delete all load-test fixture users and their analyses (cascade).'

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='Required. Confirms the delete.')
        parser.add_argument('--force', action='store_true', help='Allow running with ENVIRONMENT=production.')

    def handle(self, *args, **options):
        guard_environment(options['force'])
        users = loadtest_users_queryset()
        count = users.count()

        if not options['yes']:
            raise CommandError(f'Would delete {count} load-test user(s) and all their analyses. Re-run with --yes.')

        deleted, per_model = users.delete()
        self.stdout.write(f'Deleted {count} load-test user(s); {deleted} row(s) total: {per_model}')
