"""Mints long-lived bearer access tokens for the load-test users.

Two reasons the load test uses bearer tokens rather than the real cookie flow:

1. LoginRateThrottle keys by IP for unauthenticated requests
   (core/throttling.py), and the `login` scope is 10/min. 100 simulated users
   logging in from one load generator would get 10 successful logins a minute.

2. accounts/authentication.py's CookieJWTAuthentication checks the
   Authorization header FIRST and only enforces CSRF for cookie-sourced auth -
   so a bearer token skips the CSRF double-submit entirely and the k6 script
   needs no cookie jar and no /api/auth/csrf/ priming.

The lifetime matters. SIMPLE_JWT sets ACCESS_TOKEN_LIFETIME to 15 minutes
(config/settings.py), and the full 0->10->50->100 profile runs ~19 minutes.
Tokens minted at setup would expire mid-run, every request would 401, and the
test would report excellent latency for an application doing nothing. So the
expiry is overridden per token here - which needs no settings change and so
cannot leak into how the real application issues tokens.

Writes a JSON array to stdout (progress goes to stderr) so it can be piped
straight into loadtest/users.json.
"""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from rest_framework_simplejwt.tokens import RefreshToken

from ._loadtest import guard_environment, loadtest_users_queryset


class Command(BaseCommand):
    help = 'Mint long-lived bearer tokens for the load-test users; prints a JSON array on stdout.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--ttl-hours', type=float, default=6.0,
            help='Access token lifetime in hours (default 6) - must outlast the whole run.',
        )
        parser.add_argument('--limit', type=int, default=0, help='Only mint for the first N users (0 = all).')
        parser.add_argument('--force', action='store_true', help='Allow running with ENVIRONMENT=production.')

    def handle(self, *args, **options):
        guard_environment(options['force'])
        lifetime = timedelta(hours=options['ttl_hours'])
        users = loadtest_users_queryset().order_by('id')
        if options['limit']:
            users = users[:options['limit']]

        payload = []
        for user in users:
            refresh = RefreshToken.for_user(user)
            access = refresh.access_token
            # Per-token override. set_exp() recomputes 'exp' from now using
            # this lifetime instead of SIMPLE_JWT's ACCESS_TOKEN_LIFETIME.
            access.set_exp(lifetime=lifetime)
            payload.append({
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'access': str(access),
                # Included so a scenario can exercise the rotation path
                # (POST /api/auth/refresh/ reads the cookie, so this is for
                # completeness/manual use rather than the k6 journey).
                'refresh': str(refresh),
            })

        if not payload:
            self.stderr.write('No load-test users found. Run create_loadtest_users first.')

        self.stderr.write(f'Minted {len(payload)} token(s), valid for {options["ttl_hours"]}h.')
        self.stdout.write(json.dumps(payload))
