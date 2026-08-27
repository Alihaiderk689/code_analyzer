"""Shared pieces for the load-test fixture commands.

Underscore-prefixed on purpose: Django's command discovery skips modules
starting with an underscore, so this is importable helper code rather than a
`manage.py _loadtest` command of its own.

These commands exist because none of the normal ways to get a test account
into this system are usable under load (see loadtest/README.md):

  - POST /api/auth/register/ sends a real Brevo email per call, is throttled
    at 5/hour per IP, and runs PwnedPasswordsValidator, which makes a live
    HTTPS call to Have I Been Pwned on every password set.
  - POST /api/auth/login/ is throttled at 10/min *per IP* (LoginRateThrottle
    keys by IP for unauthenticated requests), so 100 simulated users cannot
    log in from one load generator.

So fixtures are built straight through the ORM, and auth is done with bearer
tokens minted offline. Nothing here touches an API endpoint.
"""
from django.conf import settings
from django.core.management.base import CommandError

# Every fixture user's email is <LOCAL_PREFIX>NNN@<EMAIL_DOMAIN> and every
# username is <USERNAME_PREFIX>NNN. Both prefixes are matched exactly by
# delete_loadtest_users, so keeping them distinctive is what makes teardown
# safe - a real user can never collide with this namespace by accident.
#
# .invalid is reserved by RFC 2606 and can never be a deliverable domain, so
# even if something did try to email one of these accounts it could not reach
# a real inbox.
EMAIL_DOMAIN = 'loadtest.invalid'
LOCAL_PREFIX = 'loadtest+'
USERNAME_PREFIX = 'loadtest_'

# Only used if you exercise the real cookie login flow by hand. The load test
# itself never sends it - it uses minted bearer tokens.
DEFAULT_PASSWORD = 'LoadTest-Fixture-9f2b!'


def email_for(index):
    return f'{LOCAL_PREFIX}{index:04d}@{EMAIL_DOMAIN}'


def username_for(index):
    return f'{USERNAME_PREFIX}{index:04d}'


def loadtest_users_queryset():
    """Every fixture user, matched by the namespace above rather than by a
    flag on the row - so a half-finished create still tears down cleanly."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(
        username__startswith=USERNAME_PREFIX,
        email__endswith=f'@{EMAIL_DOMAIN}',
    )


def guard_environment(force=False):
    """Refuses to run against ENVIRONMENT=production without an explicit --force.

    These commands create fake users, write thousands of rows, and (in the
    delete case) remove data permanently. None of that belongs in a production
    database, and the cost of the guard is one flag when someone genuinely
    means it.
    """
    if getattr(settings, 'ENVIRONMENT', 'development') == 'production' and not force:
        raise CommandError(
            'Refusing to run a load-test fixture command with ENVIRONMENT=production. '
            'These commands create fake users and bulk-write/delete rows. '
            'Re-run with --force only if you are certain this is not a real database.'
        )
