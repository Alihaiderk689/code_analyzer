"""Creates the dedicated load-test user accounts, straight through the ORM.

Deliberately does not go near /api/auth/register/ - see _loadtest.py for why
(Brevo sends, a 5/hour IP throttle, and a live Have I Been Pwned lookup on
every password set). `set_password()` does not run Django's password
validators, so no HTTPS call leaves this process.

One account per simulated user is not a nicety: DRF's UserRateThrottle is
300/min keyed by user id (config/settings.py), so 100 VUs sharing one account
would share one bucket and the test would measure the throttle instead of the
application.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from ._loadtest import DEFAULT_PASSWORD, email_for, guard_environment, username_for


class Command(BaseCommand):
    help = 'Create (idempotently) the dedicated load-test user accounts. Never touches the registration API.'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=100, help='How many fixture users to ensure exist (default 100).')
        parser.add_argument('--password', default=DEFAULT_PASSWORD, help='Password to set on each fixture user.')
        parser.add_argument('--force', action='store_true', help='Allow running with ENVIRONMENT=production.')

    def handle(self, *args, **options):
        guard_environment(options['force'])
        count = options['count']
        password = options['password']
        User = get_user_model()

        created = 0
        existing = 0
        for index in range(count):
            username = username_for(index)
            email = email_for(index)
            with transaction.atomic():
                user, was_created = User.objects.get_or_create(
                    username=username,
                    defaults={'email': email, 'first_name': 'Load', 'last_name': f'Test {index:04d}'},
                )
                # Reset the password every run, not just on create, so a
                # re-run with a different --password is not silently ignored.
                user.email = email
                user.is_active = True
                user.set_password(password)
                user.save()

                # Profile is auto-created by accounts/signals.py's post_save
                # receiver; this just marks it verified. Login does not check
                # is_verified (EmailLoginSerializer), but the frontend gates on
                # it, so verified is the state a realistic session is actually in.
                profile = user.profile
                if not profile.is_verified:
                    profile.is_verified = True
                    profile.save(update_fields=['is_verified'])

            created += was_created
            existing += not was_created

        self.stdout.write(f'Load-test users ready: {created} created, {existing} already existed (total {count}).')
