import re
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from django.conf import settings
from django.core.cache.backends.redis import RedisCache
from django.core.exceptions import ImproperlyConfigured
from django.db import InterfaceError, OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse_lazy
from django.views.static import serve as django_serve
from redis.exceptions import RedisError

from analyses.services.bandit_service import BANDIT_TIMEOUT_SECONDS

from .cache import ResilientRedisCache
from .exceptions import custom_exception_handler
from .settings_validation import validate_allowed_hosts


class CustomExceptionHandlerTests(TestCase):
    """DRF's default exception_handler returns None for anything that isn't an
    APIException/Http404/PermissionDenied - that's the gap custom_exception_handler
    closes, so every unhandled exception still gets a JSON body instead of
    Django's HTML 500 page."""

    def test_unhandled_exception_returns_generic_500_json(self):
        with override_settings(DEBUG=False):
            response = custom_exception_handler(RuntimeError('boom'), {'request': None})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data, {'detail': 'An unexpected error occurred. Please try again later.'})

    def test_unhandled_exception_includes_detail_in_debug(self):
        with override_settings(DEBUG=True):
            response = custom_exception_handler(RuntimeError('boom'), {'request': None})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data['detail'], 'boom')
        self.assertEqual(response.data['exception'], 'RuntimeError')

    def test_known_drf_exceptions_still_handled_normally(self):
        from rest_framework.exceptions import NotFound

        response = custom_exception_handler(NotFound('missing'), {'request': None})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'missing')


class SecurityHeaderTests(TestCase):
    """Covers both halves of the response-header hardening: the ones Django's
    SecurityMiddleware emits from settings, and the CSP/Permissions-Policy that
    core.middleware.SecurityHeadersMiddleware adds because Django 4.2 has no
    setting for them."""

    # Any AllowAny endpoint works; this one is the cheapest (no DB, no auth).
    api_url = reverse_lazy('auth-csrf')

    def test_api_response_gets_the_strict_csp(self):
        response = self.client.get(self.api_url)

        csp = response.headers['Content-Security-Policy']
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("form-action 'none'", csp)

    # The default manifest storage would need a collectstatic run before
    # admin's own {% static %} tags resolve; the plain backend renders the
    # same page without one. Nothing about the header logic depends on it.
    @override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
    def test_html_response_gets_the_relaxed_csp_but_still_forbids_framing(self):
        """Django admin, DRF's browsable API and the analysis HTML report all
        need same-origin assets and inline styles, so they can't take the
        strict policy - but framing stays denied for them too."""
        response = self.client.get('/admin/login/')

        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        csp = response.headers['Content-Security-Policy']
        self.assertIn("default-src 'self'", csp)
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_permissions_policy_denies_unused_features(self):
        policy = self.client.get(self.api_url).headers['Permissions-Policy']

        for feature in ('camera', 'geolocation', 'microphone', 'payment', 'usb'):
            self.assertIn(f'{feature}=()', policy)

    def test_nosniff_and_referrer_policy_and_frame_options_present(self):
        response = self.client.get(self.api_url)

        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Referrer-Policy'], 'strict-origin-when-cross-origin')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_no_hsts_outside_production_even_over_https(self):
        """HSTS is scoped to ENVIRONMENT=production. The plain-http case is
        uninteresting (SecurityMiddleware never emits HSTS there regardless);
        the case that matters is an https dev server or a TLS-terminating
        tunnel in front of runserver, which would otherwise pin the developer's
        hostname to https for a year."""
        self.assertEqual(settings.SECURE_HSTS_SECONDS, 0)

        secure = self.client.get(self.api_url, secure=True)

        self.assertNotIn('Strict-Transport-Security', secure.headers)

    @override_settings(SECURE_HSTS_SECONDS=31536000, SECURE_HSTS_INCLUDE_SUBDOMAINS=True)
    def test_production_hsts_values_emitted_on_secure_requests(self):
        """The production configuration, exercised directly - the test suite
        runs as ENVIRONMENT=development, so the settings are applied here
        rather than read from the module."""
        hsts = self.client.get(self.api_url, secure=True).headers['Strict-Transport-Security']

        self.assertIn('max-age=31536000', hsts)
        self.assertIn('includeSubDomains', hsts)

    def test_hsts_preload_not_enabled(self):
        """Preload is a one-way door - it belongs to a deliberate decision about
        a specific domain, not to every deployment of this codebase."""
        self.assertFalse(settings.SECURE_HSTS_PRELOAD)

    @override_settings(SECURE_HSTS_SECONDS=31536000, SECURE_HSTS_INCLUDE_SUBDOMAINS=True)
    def test_hsts_never_sent_over_plain_http(self):
        plain = self.client.get(self.api_url)

        self.assertNotIn('Strict-Transport-Security', plain.headers)

    def test_headers_are_set_even_when_a_request_never_reaches_a_view(self):
        """The middleware is outermost precisely so short-circuited responses
        (404s, redirects, CSRF rejections) aren't served without a CSP."""
        response = self.client.get('/no-such-path-exists/')

        self.assertEqual(response.status_code, 404)
        self.assertIn('Content-Security-Policy', response.headers)
        self.assertIn('Permissions-Policy', response.headers)


class AllowedHostsValidationTests(TestCase):
    """Guards the production Host-header allowlist. Unit tests against
    validate_allowed_hosts() directly rather than re-importing config.settings
    under a doctored environment - the rule is the thing worth pinning, and it
    lives in its own module precisely so it can be called like this."""

    def test_production_rejects_the_wildcard(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            validate_allowed_hosts(['*'], 'production')

        self.assertIn('*', str(ctx.exception))

    def test_production_rejects_the_wildcard_alongside_real_hosts(self):
        """A wildcard anywhere in the list disables Host validation for the
        whole list, so it can't be excused by the presence of real entries."""
        with self.assertRaises(ImproperlyConfigured):
            validate_allowed_hosts(['api.example.com', '*'], 'production')

    def test_production_rejects_an_empty_list(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_allowed_hosts([], 'production')

    def test_production_accepts_an_explicit_allowlist(self):
        hosts = ['api.example.com', 'example.com']

        self.assertEqual(validate_allowed_hosts(hosts, 'production'), hosts)

    def test_production_accepts_leading_dot_subdomain_form(self):
        """'.example.com' matches the domain and its subdomains and is still an
        explicit allowlist entry - it is not the '*' wildcard."""
        self.assertEqual(validate_allowed_hosts(['.example.com'], 'production'), ['.example.com'])

    def test_development_is_left_alone(self):
        """Development keeps Django's own behavior so no local configuration is
        required to run the server."""
        self.assertEqual(validate_allowed_hosts([], 'development'), [])
        self.assertEqual(validate_allowed_hosts(['*'], 'development'), ['*'])

    def test_configured_allowed_hosts_is_not_a_wildcard(self):
        self.assertNotIn('*', settings.ALLOWED_HOSTS)


class HealthEndpointTests(TestCase):
    """Liveness and readiness are deliberately different endpoints - see
    core/views.py. The split exists so a transient database outage reports
    honestly without making the platform restart-loop every container."""

    liveness_url = reverse_lazy('health-check')
    readiness_url = reverse_lazy('readiness-check')

    def test_liveness_is_always_ok(self):
        response = self.client.get(self.liveness_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'status': 'ok'})

    def test_liveness_stays_ok_when_the_database_is_down(self):
        """This is the point of the split: render.yaml's healthCheckPath points
        here, and a DB blip must not trigger a restart that cannot fix it."""
        with patch('core.views.connection.cursor', side_effect=OperationalError('down')):
            response = self.client.get(self.liveness_url)

        self.assertEqual(response.status_code, 200)

    def test_readiness_ok_when_dependencies_are_up(self):
        response = self.client.get(self.readiness_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')
        self.assertTrue(response.data['checks']['database']['ok'])

    def test_readiness_503_when_the_database_is_down(self):
        """The regression this closes: the old single endpoint returned a flat
        'ok' straight through a total database outage."""
        with patch('core.views.connection.cursor', side_effect=OperationalError('down')):
            response = self.client.get(self.readiness_url)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['status'], 'unavailable')
        self.assertFalse(response.data['checks']['database']['ok'])
        self.assertEqual(response.data['checks']['database']['error'], 'OperationalError')

    def test_readiness_degraded_but_available_when_only_the_cache_is_down(self):
        """Cache failure must not read as unready: throttles fail open, so
        requests still succeed without Redis."""
        # Patch the module-level name, not a method on the shared cache object:
        # DRF's throttling imports the same object, so mutating it would break
        # the throttle check before the request ever reaches the view.
        broken_cache = Mock()
        broken_cache.set.side_effect = RuntimeError('redis down')
        with patch('core.views.cache', broken_cache):
            response = self.client.get(self.readiness_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'degraded')
        self.assertTrue(response.data['checks']['database']['ok'])
        self.assertFalse(response.data['checks']['cache']['ok'])


class DatabaseUnavailableHandlingTests(TestCase):
    """A dropped connection is a dependency outage, not a bug in this code."""

    def test_operational_error_returns_503_not_500(self):
        response = custom_exception_handler(OperationalError('connection lost'), {'request': None})

        self.assertEqual(response.status_code, 503)
        self.assertIn('temporarily unavailable', response.data['detail'])

    def test_interface_error_returns_503(self):
        response = custom_exception_handler(InterfaceError('connection already closed'), {'request': None})

        self.assertEqual(response.status_code, 503)

    def test_other_exceptions_still_return_500(self):
        with override_settings(DEBUG=False):
            response = custom_exception_handler(RuntimeError('boom'), {'request': None})

        self.assertEqual(response.status_code, 500)


class CacheConfigurationTests(TestCase):
    """Throttle counters live in this cache. Per-process LocMemCache meant each
    gunicorn worker kept its own, so the effective limit was rate x workers."""

    def test_resilient_cache_treats_redis_errors_as_a_miss(self):
        """Fail open, deliberately: the cache backs rate limiting, not
        authorization, and a raising backend would turn a Redis outage into a
        500 on every endpoint. See core/cache.py for why that is safe here."""
        backend = ResilientRedisCache('redis://localhost:6379/0', {})

        with patch.object(RedisCache, 'get', side_effect=RedisError('no connection')):
            self.assertIsNone(backend.get('some-key'))
        with patch.object(RedisCache, 'set', side_effect=RedisError('no connection')):
            self.assertIsNone(backend.set('some-key', 'value'))
        with patch.object(RedisCache, 'incr', side_effect=RedisError('no connection')):
            self.assertIsNone(backend.incr('some-key'))

    def test_resilient_cache_passes_through_when_redis_is_healthy(self):
        backend = ResilientRedisCache('redis://localhost:6379/0', {})

        with patch.object(RedisCache, 'get', return_value='cached') as mocked:
            self.assertEqual(backend.get('some-key'), 'cached')
        mocked.assert_called_once()


class RenderDeploymentConfigTests(SimpleTestCase):
    """Guards the config mistake that stopped the Celery worker booting.

    validate_allowed_hosts (correctly) rejects an empty ALLOWED_HOSTS in
    production for *any* process, including one that serves no HTTP. When
    ALLOWED_HOSTS lived only on the web service, the worker inherited nothing
    and crash-looped - while webhooks kept returning 202, so nothing surfaced.
    """

    render_yaml = Path(settings.BASE_DIR).parent / 'render.yaml'

    def _config(self):
        return yaml.safe_load(self.render_yaml.read_text())

    def test_allowed_hosts_is_in_the_shared_env_group(self):
        groups = {g['name']: g for g in self._config()['envVarGroups']}
        keys = {v['key'] for v in groups['code-analyzer-shared']['envVars']}

        self.assertIn('ALLOWED_HOSTS', keys)

    def test_every_service_inherits_the_shared_group(self):
        for service in self._config()['services']:
            if service['type'] == 'keyvalue':
                continue
            inherits = any('fromGroup' in v for v in service.get('envVars', []))
            self.assertTrue(inherits, f"{service['name']} does not inherit code-analyzer-shared")

    def test_web_service_has_a_disk_for_uploaded_media(self):
        """Without it MEDIA_ROOT is ephemeral and avatars vanish on redeploy."""
        web = next(s for s in self._config()['services'] if s['type'] == 'web')

        self.assertEqual(web['disk']['mountPath'], '/app/media')

    @staticmethod
    def _gunicorn_timeout():
        dockerfile = (Path(settings.BASE_DIR) / 'Dockerfile').read_text()
        cmd = next(line for line in dockerfile.splitlines() if line.startswith('CMD ['))
        return int(re.search(r'"--timeout", "(\d+)"', cmd).group(1))

    def test_gunicorn_timeout_exceeds_the_bounded_ai_chain(self):
        """The two must move together: raising the timeout without bounding the
        providers just lets one request hold a worker for minutes."""
        self.assertGreater(self._gunicorn_timeout(), 3 * settings.AI_REQUEST_TIMEOUT_SECONDS)

    def test_gunicorn_timeout_exceeds_the_worst_ai_inclusive_request(self):
        """The AI chain is not the longest synchronous path - a security scan
        runs Bandit *and then* the full AI chain in one request, so the budget
        that actually has to fit under gunicorn's timeout is the sum.

        Guards a margin that is thinner than it looks: raising either
        BANDIT_TIMEOUT_SECONDS or AI_REQUEST_TIMEOUT_SECONDS without raising
        --timeout turns a slow security scan into a 502.
        """
        worst_case = BANDIT_TIMEOUT_SECONDS + 3 * settings.AI_REQUEST_TIMEOUT_SECONDS

        self.assertGreater(
            self._gunicorn_timeout(), worst_case,
            'gunicorn --timeout must exceed bandit + the full AI fallback chain',
        )


class MediaServingTests(SimpleTestCase):
    """Avatar URLs 404'd in production: the /media/ route was DEBUG-gated, and
    the Render deployment has no nginx in front of gunicorn to serve it."""

    def test_media_url_is_routed_regardless_of_debug(self):
        with override_settings(DEBUG=False):
            match = resolve(f'{settings.MEDIA_URL}avatars/2026/01/example.png')

        self.assertEqual(match.func, django_serve)
