import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.signing import dumps as signing_dumps
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from pwned_passwords_django.exceptions import ErrorCode as PwnedPasswordsErrorCode
from pwned_passwords_django.exceptions import PwnedPasswordsError
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.throttling import LoginRateThrottle, OtpVerifyAccountRateThrottle, OtpVerifyRateThrottle
from github_integration.services.oauth_service import _STATE_SALT

from .brevo_client import BrevoAPIError
from .cookies import ACCESS_COOKIE, REFRESH_COOKIE
from .google_auth import GoogleTokenError, verify_google_access_token
from .models import PendingRegistration, Profile
from .otp import OTP_MAX_ATTEMPTS, hash_otp_code, issue_otp, issue_otp_to, verify_otp
from .password_validation import PwnedPasswordsValidator
from .tokens import revoke_all_refresh_tokens

User = get_user_model()


def make_user(email='user@example.com', password='TestPass123!', verified=False):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=password)
    user.profile.is_verified = verified
    user.profile.save(update_fields=['is_verified'])
    return user


class RegisterTests(APITestCase):
    url = reverse('auth-register')

    @patch('accounts.emails.BrevoClient')
    def test_register_creates_no_account_only_a_pending_attempt(self, mock_brevo_cls):
        response = self.client.post(self.url, {
            'email': 'new@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The whole point: registering reserves nothing in the User table, so
        # an address typed by someone who cannot read that inbox is not burned.
        self.assertFalse(User.objects.filter(email='new@example.com').exists())
        pending = PendingRegistration.objects.get(email='new@example.com')
        self.assertIsNotNone(pending.otp_code_hash)
        self.assertNotEqual(pending.password, 'TestPass123!')
        mock_brevo_cls.return_value.send_email.assert_called_once()
        self.assertEqual(mock_brevo_cls.return_value.send_email.call_args.kwargs['to_email'], 'new@example.com')

    @patch('accounts.emails.BrevoClient')
    def test_verifying_the_code_creates_the_account(self, mock_brevo_cls):
        self.client.post(self.url, {
            'email': 'earned@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })
        pending = PendingRegistration.objects.get(email='earned@example.com')
        code = issue_otp_to(pending)

        response = self.client.post(reverse('auth-verify-email'), {'email': 'earned@example.com', 'code': code})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(email='earned@example.com')
        self.assertTrue(user.is_active)
        self.assertTrue(user.profile.is_verified)
        # The password survived the round trip through PendingRegistration.
        self.assertTrue(user.check_password('TestPass123!'))
        self.assertFalse(PendingRegistration.objects.filter(email='earned@example.com').exists())

    @patch('accounts.emails.BrevoClient')
    def test_reregistering_an_unverified_address_replaces_the_attempt(self, mock_brevo_cls):
        for _ in range(2):
            response = self.client.post(self.url, {
                'email': 'again@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
            })
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Someone who lost the first email is not in conflict with themselves -
        # they get a fresh code, not "this email is already registered".
        self.assertEqual(PendingRegistration.objects.filter(email='again@example.com').count(), 1)

    @patch('accounts.emails.BrevoClient')
    def test_register_reclaims_an_address_stranded_by_the_old_flow(self, mock_brevo_cls):
        stranded = make_user(email='stranded@example.com', verified=False)
        stranded.is_active = False
        stranded.save(update_fields=['is_active'])

        response = self.client.post(self.url, {
            'email': 'stranded@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })

        # Without this the address is unclaimable forever: its owner can
        # neither log in (inactive) nor register again (already exists).
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(User.objects.filter(pk=stranded.pk).exists())
        self.assertTrue(PendingRegistration.objects.filter(email='stranded@example.com').exists())

    def test_register_rejected_when_a_verified_account_holds_the_address(self):
        user = make_user(email='taken@example.com', verified=True)
        self.assertTrue(user.is_active)
        response = self.client.post(self.url, {
            'email': 'taken@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    @patch('accounts.emails.BrevoClient')
    def test_register_rolls_back_when_email_send_fails(self, mock_brevo_cls):
        mock_brevo_cls.return_value.send_email.side_effect = BrevoAPIError('boom')
        response = self.client.post(self.url, {
            'email': 'failed@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(User.objects.filter(email='failed@example.com').exists())
        # Nor a pending attempt holding a code nobody received.
        self.assertFalse(PendingRegistration.objects.filter(email='failed@example.com').exists())

    def test_register_duplicate_email_rejected(self):
        make_user(email='dup@example.com')
        response = self.client.post(self.url, {
            'email': 'dup@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_password_mismatch_rejected(self):
        response = self.client.post(self.url, {
            'email': 'mismatch@example.com', 'password': 'TestPass123!', 'password2': 'Different123!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn('mismatch@example.com', User.objects.values_list('email', flat=True))

    def test_register_weak_password_rejected(self):
        response = self.client.post(self.url, {
            'email': 'weak@example.com', 'password': 'password', 'password2': 'password',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class LoginTests(APITestCase):
    url = reverse('auth-login')

    def test_login_success_sets_httponly_cookies_and_returns_user(self):
        make_user(email='login@example.com', password='TestPass123!', verified=True)
        response = self.client.post(self.url, {'email': 'login@example.com', 'password': 'TestPass123!'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Tokens must never appear in the JSON body - only in httpOnly cookies,
        # otherwise JS could just read them there regardless of httponly.
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)
        self.assertEqual(response.data['user']['email'], 'login@example.com')
        self.assertTrue(response.data['user']['is_verified'])

        access_cookie = response.cookies[ACCESS_COOKIE]
        self.assertTrue(access_cookie.value)
        self.assertTrue(access_cookie['httponly'])
        refresh_cookie = response.cookies[REFRESH_COOKIE]
        self.assertTrue(refresh_cookie.value)
        self.assertTrue(refresh_cookie['httponly'])

    def test_login_wrong_password_rejected(self):
        make_user(email='login2@example.com', password='TestPass123!')
        response = self.client.post(self.url, {'email': 'login2@example.com', 'password': 'WrongPass!'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_nonexistent_email_rejected(self):
        response = self.client.post(self.url, {'email': 'nobody@example.com', 'password': 'TestPass123!'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_inactive_user_rejected(self):
        user = make_user(email='inactive@example.com', password='TestPass123!')
        user.is_active = False
        user.save(update_fields=['is_active'])
        response = self.client.post(self.url, {'email': 'inactive@example.com', 'password': 'TestPass123!'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


GOOGLE_CLAIMS = {
    'sub': 'google-sub-123',
    'email': 'g@example.com',
    'email_verified': True,
    'given_name': 'Ada',
    'family_name': 'Lovelace',
}


class GoogleLoginTests(APITestCase):
    url = reverse('auth-google-login')

    @patch('accounts.serializers.verify_google_access_token', return_value=GOOGLE_CLAIMS)
    def test_new_user_created_and_cookies_set(self, mock_verify):
        response = self.client.post(self.url, {'access_token': 'fake'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)
        user = User.objects.get(email='g@example.com')
        self.assertEqual(user.profile.google_id, 'google-sub-123')
        self.assertTrue(user.profile.is_verified)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.first_name, 'Ada')
        self.assertTrue(response.cookies[ACCESS_COOKIE]['httponly'])

    @patch('accounts.serializers.verify_google_access_token', return_value=GOOGLE_CLAIMS)
    def test_auto_links_existing_verified_email(self, mock_verify):
        make_user(email='g@example.com', verified=False)
        response = self.client.post(self.url, {'access_token': 'fake'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(email='g@example.com')
        self.assertEqual(user.profile.google_id, 'google-sub-123')
        self.assertTrue(user.profile.is_verified)

    @patch('accounts.serializers.verify_google_access_token', return_value=GOOGLE_CLAIMS)
    def test_repeat_login_reuses_same_user(self, mock_verify):
        self.client.post(self.url, {'access_token': 'fake'})
        response = self.client.post(self.url, {'access_token': 'fake'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.filter(email='g@example.com').count(), 1)

    @patch(
        'accounts.serializers.verify_google_access_token',
        return_value={**GOOGLE_CLAIMS, 'email_verified': False},
    )
    def test_unverified_email_does_not_autolink(self, mock_verify):
        make_user(email='g@example.com')
        response = self.client.post(self.url, {'access_token': 'fake'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(User.objects.get(email='g@example.com').profile.google_id)

    @patch('accounts.serializers.verify_google_access_token')
    def test_conflicting_google_id_rejected(self, mock_verify):
        mock_verify.return_value = GOOGLE_CLAIMS
        self.client.post(self.url, {'access_token': 'fake'})  # links google-sub-123 to g@example.com

        mock_verify.return_value = {**GOOGLE_CLAIMS, 'sub': 'google-sub-999'}
        response = self.client.post(self.url, {'access_token': 'fake2'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('accounts.serializers.verify_google_access_token', side_effect=GoogleTokenError('bad token'))
    def test_invalid_credential_rejected(self, mock_verify):
        response = self.client.post(self.url, {'access_token': 'garbage'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(GOOGLE_CLIENT_ID='test-google-client-id')
class VerifyGoogleAccessTokenTests(TestCase):
    """Direct unit tests for verify_google_access_token itself (the two-call
    tokeninfo+userinfo flow), not just the serializer-level mock
    GoogleLoginTests above uses."""

    @staticmethod
    def _mock_response(ok=True, json_data=None):
        response = Mock()
        response.ok = ok
        response.json.return_value = json_data or {}
        return response

    @patch('accounts.google_auth.requests.get')
    def test_returns_claims_on_success(self, mock_get):
        mock_get.side_effect = [
            self._mock_response(json_data={'aud': 'test-google-client-id', 'sub': '123'}),
            self._mock_response(json_data={
                'sub': '123', 'email': 'g@example.com', 'email_verified': True,
                'given_name': 'Ada', 'family_name': 'Lovelace',
            }),
        ]
        claims = verify_google_access_token('token123')
        self.assertEqual(claims, {
            'sub': '123', 'email': 'g@example.com', 'email_verified': True,
            'given_name': 'Ada', 'family_name': 'Lovelace',
        })

    @patch('accounts.google_auth.requests.get')
    def test_wrong_audience_rejected(self, mock_get):
        mock_get.return_value = self._mock_response(json_data={'aud': 'someone-elses-client-id', 'sub': '123'})
        with self.assertRaises(GoogleTokenError):
            verify_google_access_token('token123')

    @patch('accounts.google_auth.requests.get')
    def test_invalid_tokeninfo_response_rejected(self, mock_get):
        mock_get.return_value = self._mock_response(ok=False)
        with self.assertRaises(GoogleTokenError):
            verify_google_access_token('token123')

    @patch('accounts.google_auth.requests.get')
    def test_userinfo_failure_rejected(self, mock_get):
        mock_get.side_effect = [
            self._mock_response(json_data={'aud': 'test-google-client-id', 'sub': '123'}),
            self._mock_response(ok=False),
        ]
        with self.assertRaises(GoogleTokenError):
            verify_google_access_token('token123')

    @override_settings(GOOGLE_CLIENT_ID='')
    def test_raises_when_not_configured(self):
        with self.assertRaises(ImproperlyConfigured):
            verify_google_access_token('token123')


_GITHUB_OAUTH_SETTINGS = dict(GITHUB_CLIENT_ID='test-client-id', GITHUB_CLIENT_SECRET='test-secret')

GITHUB_CLAIMS = {
    'github_id': 4242,
    'username': 'octocat',
    'email': 'g@example.com',
    'avatar_url': 'https://example.com/a.png',
}


def _github_login_state():
    return signing_dumps({'purpose': 'login', 'nonce': 'test-nonce'}, salt=_STATE_SALT)


@override_settings(**_GITHUB_OAUTH_SETTINGS)
class GitHubLoginInitiateViewTests(APITestCase):
    url = reverse('auth-github-login')

    def test_returns_authorize_url_with_narrow_scope(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('authorize_url', response.data)
        self.assertIn('scope=read%3Auser+user%3Aemail', response.data['authorize_url'])

    @override_settings(GITHUB_CLIENT_ID='', GITHUB_CLIENT_SECRET='')
    def test_returns_503_when_not_configured(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class GitHubAccountLoginTests(APITestCase):
    url = reverse('github-callback')

    @patch('github_integration.oauth_views.GitHubOAuthService.complete_login_oauth', return_value=GITHUB_CLAIMS)
    def test_new_user_created_and_cookies_set(self, mock_complete):
        response = self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(response.url.endswith('/dashboard'))
        user = User.objects.get(email='g@example.com')
        self.assertEqual(user.profile.github_id, '4242')
        self.assertTrue(user.profile.is_verified)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(response.cookies[ACCESS_COOKIE]['httponly'])

    @patch('github_integration.oauth_views.GitHubOAuthService.complete_login_oauth', return_value=GITHUB_CLAIMS)
    def test_auto_links_existing_verified_email_and_staff_goes_to_admin(self, mock_complete):
        User.objects.create_user(username='existingstaff', email='g@example.com', is_staff=True)

        response = self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})

        self.assertTrue(response.url.endswith('/admin'))
        user = User.objects.get(email='g@example.com')
        self.assertEqual(user.profile.github_id, '4242')

    @patch('github_integration.oauth_views.GitHubOAuthService.complete_login_oauth', return_value=GITHUB_CLAIMS)
    def test_repeat_login_reuses_same_user(self, mock_complete):
        self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})
        response = self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(User.objects.filter(email='g@example.com').count(), 1)

    @patch(
        'github_integration.oauth_views.GitHubOAuthService.complete_login_oauth',
        return_value={**GITHUB_CLAIMS, 'email': None},
    )
    def test_no_verified_email_redirects_with_error(self, mock_complete):
        response = self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})
        self.assertIn('error=email_not_verified', response.url)

    @patch('github_integration.oauth_views.GitHubOAuthService.complete_login_oauth')
    def test_conflicting_github_id_rejected(self, mock_complete):
        mock_complete.return_value = GITHUB_CLAIMS
        self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})  # links 4242 to g@example.com

        mock_complete.return_value = {**GITHUB_CLAIMS, 'github_id': 9999}
        response = self.client.get(self.url, {'code': 'abc', 'state': _github_login_state()})
        self.assertIn('error=account_conflict', response.url)


class LogoutTests(APITestCase):
    def _login(self):
        make_user(email='logout@example.com', password='TestPass123!')
        self.client.post(reverse('auth-login'), {'email': 'logout@example.com', 'password': 'TestPass123!'})

    def test_logout_blacklists_refresh_token_and_clears_cookies(self):
        self._login()
        refresh_value = self.client.cookies[REFRESH_COOKIE].value

        response = self.client.post(reverse('auth-logout'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.cookies[ACCESS_COOKIE].value, '')
        self.assertEqual(response.cookies[REFRESH_COOKIE].value, '')

        # Logout just cleared this client's own cookie jar, so replay the
        # captured pre-logout value directly - proves the token itself was
        # blacklisted server-side, not just that the cookie is gone locally.
        self.client.cookies[REFRESH_COOKIE] = refresh_value
        refresh_response = self.client.post(reverse('auth-refresh'))
        self.assertEqual(refresh_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_without_refresh_cookie_still_succeeds(self):
        # Logout still requires a valid access token (IsAuthenticated, same as
        # before) - but with one, a *missing* refresh cookie (e.g. it already
        # expired, or a client that never had one) is a no-op, not an error.
        self._login()
        del self.client.cookies[REFRESH_COOKIE]
        response = self.client.post(reverse('auth-logout'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_logout_with_garbage_refresh_cookie_still_succeeds(self):
        self._login()
        self.client.cookies[REFRESH_COOKIE] = 'not-a-real-token'
        response = self.client.post(reverse('auth-logout'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CookieAuthCSRFTests(APITestCase):
    """APITestCase's default client doesn't enforce CSRF (enforce_csrf_checks=
    False), which is why none of the other tests above need to think about it.
    These use a client that does, to prove CookieJWTAuthentication's CSRF
    double-submit check is actually wired up and not just present in name."""

    def _login(self, client, email='csrf@example.com'):
        make_user(email=email, password='TestPass123!')
        response = client.post(reverse('auth-login'), {'email': email, 'password': 'TestPass123!'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unsafe_request_without_csrf_token_rejected(self):
        client = APIClient(enforce_csrf_checks=True)
        self._login(client)

        response = client.post(reverse('auth-change-password'), {
            'old_password': 'TestPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unsafe_request_with_valid_csrf_token_succeeds(self):
        client = APIClient(enforce_csrf_checks=True)
        self._login(client, email='csrf2@example.com')

        csrf_response = client.get(reverse('auth-csrf'))
        self.assertEqual(csrf_response.status_code, status.HTTP_200_OK)
        csrf_token = client.cookies['csrftoken'].value

        response = client.post(
            reverse('auth-change-password'),
            {'old_password': 'TestPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!'},
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unsafe_request_with_body_csrf_token_succeeds(self):
        """The frontend's primary source for X-CSRFToken is the /auth/csrf/
        response body, not document.cookie (see api.js's primeCsrf()) - that's
        what makes CSRF work even when frontend/backend can't share a
        registrable domain. Django masks the secret differently on every
        get_token() call, so the body value is never byte-identical to the
        cookie value (see CsrfCookieViewTests.test_also_returns_token_in_body)
        - it must still be accepted on its own, unmasked-secret comparison."""
        client = APIClient(enforce_csrf_checks=True)
        self._login(client, email='csrf3@example.com')

        csrf_response = client.get(reverse('auth-csrf'))
        body_token = csrf_response.data['csrfToken']
        self.assertNotEqual(body_token, client.cookies['csrftoken'].value)

        response = client.post(
            reverse('auth-change-password'),
            {'old_password': 'TestPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!'},
            HTTP_X_CSRFTOKEN=body_token,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bearer_header_auth_does_not_require_csrf(self):
        """A non-cookie client (script, mobile app) using the Authorization
        header directly is unaffected by CSRF enforcement - only browser
        cookie auth needs it, since only cookies are attached automatically
        by something other than the client's own code."""
        make_user(email='bearer@example.com', password='TestPass123!')
        self.client.post(reverse('auth-login'), {'email': 'bearer@example.com', 'password': 'TestPass123!'})
        access_token = self.client.cookies[ACCESS_COOKIE].value

        client = APIClient(enforce_csrf_checks=True)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')
        response = client.post(reverse('auth-change-password'), {
            'old_password': 'TestPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class RefreshViewTests(APITestCase):
    def _login(self):
        make_user(email='refresh@example.com', password='TestPass123!')
        self.client.post(reverse('auth-login'), {'email': 'refresh@example.com', 'password': 'TestPass123!'})

    def test_refresh_rotates_cookies(self):
        self._login()
        old_access = self.client.cookies[ACCESS_COOKIE].value
        old_refresh = self.client.cookies[REFRESH_COOKIE].value

        response = self.client.post(reverse('auth-refresh'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Never leaks the token into the body - cookies only, same as login.
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)

        new_access = response.cookies[ACCESS_COOKIE].value
        new_refresh = response.cookies[REFRESH_COOKIE].value
        self.assertTrue(new_access)
        self.assertTrue(new_refresh)
        self.assertNotEqual(new_access, old_access)
        self.assertNotEqual(new_refresh, old_refresh)  # ROTATE_REFRESH_TOKENS=True

        # The rotated-out refresh token is now blacklisted (BLACKLIST_AFTER_ROTATION=True).
        self.client.cookies[REFRESH_COOKIE] = old_refresh
        reuse_response = self.client.post(reverse('auth-refresh'))
        self.assertEqual(reuse_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_without_cookie_401(self):
        response = self.client.post(reverse('auth-refresh'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_with_garbage_cookie_401_and_clears_cookies(self):
        self.client.cookies[REFRESH_COOKIE] = 'not-a-real-token'
        response = self.client.post(reverse('auth-refresh'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.cookies[ACCESS_COOKIE].value, '')
        self.assertEqual(response.cookies[REFRESH_COOKIE].value, '')

    def test_refresh_ignores_body_and_only_trusts_cookie(self):
        """The whole point of moving to cookies is that the frontend never
        handles the raw token - a refresh token in the body must be ignored,
        not treated as a trusted alternative source."""
        self._login()
        response = self.client.post(reverse('auth-refresh'), {'refresh': 'not-a-real-token'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CsrfCookieViewTests(APITestCase):
    def test_sets_readable_csrf_cookie(self):
        response = self.client.get(reverse('auth-csrf'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        csrf_cookie = response.cookies['csrftoken']
        self.assertTrue(csrf_cookie.value)
        # Must be JS-readable (not httponly) - same-site/subdomain deployments
        # can still read it directly via document.cookie.
        self.assertFalse(csrf_cookie['httponly'])

    def test_also_returns_token_in_body(self):
        # The body is the primary channel the frontend relies on (see
        # api.js's primeCsrf()) - document.cookie can't be read across
        # genuinely unrelated domains, so the token must be handed over
        # directly rather than requiring the frontend to parse the cookie.
        # Not byte-identical to the cookie value - see
        # CookieAuthCSRFTests.test_unsafe_request_with_body_csrf_token_succeeds
        # for why that's expected and still valid.
        response = self.client.get(reverse('auth-csrf'))
        self.assertTrue(response.data['csrfToken'])


class ForgotPasswordTests(APITestCase):
    url = reverse('auth-forgot-password')

    @patch('accounts.emails.BrevoClient')
    def test_forgot_password_existing_user_sends_email(self, mock_brevo_cls):
        make_user(email='forgot@example.com')
        response = self.client.post(self.url, {'email': 'forgot@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_brevo_cls.return_value.send_email.assert_called_once()
        call_kwargs = mock_brevo_cls.return_value.send_email.call_args.kwargs
        self.assertIn('reset-password', call_kwargs['html_content'])

    @patch('accounts.emails.BrevoClient')
    def test_forgot_password_nonexistent_user_same_response(self, mock_brevo_cls):
        response = self.client.post(self.url, {'email': 'ghost@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['detail'], 'If an account with that email exists, a reset link has been sent.')
        mock_brevo_cls.return_value.send_email.assert_not_called()


class ResetPasswordTests(APITestCase):
    url = reverse('auth-reset-password')

    def _link_params(self, user):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return uid, token

    def test_reset_password_valid_token_changes_password(self):
        user = make_user(email='reset@example.com', password='OldPass123!')
        uid, token = self._link_params(user)

        response = self.client.post(self.url, {
            'uid': uid, 'token': token, 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        login = self.client.post(reverse('auth-login'), {'email': 'reset@example.com', 'password': 'NewPass456!'})
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        old_login = self.client.post(reverse('auth-login'), {'email': 'reset@example.com', 'password': 'OldPass123!'})
        self.assertEqual(old_login.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_invalid_token_rejected(self):
        user = make_user(email='reset2@example.com')
        uid, _ = self._link_params(user)

        response = self.client.post(self.url, {
            'uid': uid, 'token': 'garbage-token', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_invalid_uid_rejected(self):
        response = self.client.post(self.url, {
            'uid': 'garbage-uid', 'token': 'garbage-token',
            'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_mismatch_rejected(self):
        user = make_user(email='reset3@example.com')
        uid, token = self._link_params(user)

        response = self.client.post(self.url, {
            'uid': uid, 'token': token, 'new_password': 'NewPass456!', 'new_password2': 'Different789!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_token_cannot_be_reused(self):
        user = make_user(email='reset4@example.com', password='OldPass123!')
        uid, token = self._link_params(user)

        first = self.client.post(self.url, {
            'uid': uid, 'token': token, 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        second = self.client.post(self.url, {
            'uid': uid, 'token': token, 'new_password': 'AnotherPass789!', 'new_password2': 'AnotherPass789!',
        })
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)


class VerifyEmailTests(APITestCase):
    url = reverse('auth-verify-email')

    def _make_inactive_user_with_otp(self, email='verify@example.com'):
        user = make_user(email=email, verified=False)
        user.is_active = False
        user.save(update_fields=['is_active'])
        code = issue_otp(user)
        return user, code

    def test_verify_email_valid_code_activates_and_verifies(self):
        user, code = self._make_inactive_user_with_otp()

        response = self.client.post(self.url, {'email': user.email, 'code': code})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.profile.is_verified)

    def test_verify_email_missing_params_400(self):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_email_wrong_code_rejected_and_increments_attempts(self):
        user, _code = self._make_inactive_user_with_otp(email='verify2@example.com')
        response = self.client.post(self.url, {'email': user.email, 'code': '000000'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(user.profile.otp_attempts, 1)

    def test_verify_email_expired_code_rejected(self):
        user, code = self._make_inactive_user_with_otp(email='verify3@example.com')
        user.profile.otp_expires_at = timezone.now() - timedelta(minutes=1)
        user.profile.save(update_fields=['otp_expires_at'])

        response = self.client.post(self.url, {'email': user.email, 'code': code})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('expired', response.data['code'][0])

    def test_verify_email_locks_out_after_max_attempts(self):
        user, code = self._make_inactive_user_with_otp(email='verify4@example.com')
        for _ in range(OTP_MAX_ATTEMPTS):
            self.client.post(self.url, {'email': user.email, 'code': '000000'})

        response = self.client.post(self.url, {'email': user.email, 'code': code})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Too many', response.data['code'][0])
        user.refresh_from_db()
        self.assertFalse(user.is_active)


class ResendVerificationTests(APITestCase):
    url = reverse('auth-resend-verification')

    @patch('accounts.emails.BrevoClient')
    def test_resend_for_unverified_user_sends_new_code(self, mock_brevo_cls):
        user = make_user(email='unverified@example.com', verified=False)
        old_hash = user.profile.otp_code_hash

        response = self.client.post(self.url, {'email': 'unverified@example.com'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_brevo_cls.return_value.send_email.assert_called_once()
        user.profile.refresh_from_db()
        self.assertIsNotNone(user.profile.otp_code_hash)
        self.assertNotEqual(user.profile.otp_code_hash, old_hash)

    @patch('accounts.emails.BrevoClient')
    def test_resend_for_already_verified_user_sends_nothing(self, mock_brevo_cls):
        make_user(email='verified@example.com', verified=True)
        response = self.client.post(self.url, {'email': 'verified@example.com'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_brevo_cls.return_value.send_email.assert_not_called()


class ChangePasswordTests(APITestCase):
    url = reverse('auth-change-password')

    def _login(self):
        user = make_user(email='change@example.com', password='OldPass123!')
        self.client.force_authenticate(user=user)
        return user

    def test_change_password_requires_authentication(self):
        response = self.client.post(self.url, {
            'old_password': 'OldPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_change_password_success(self):
        self._login()
        response = self.client.post(self.url, {
            'old_password': 'OldPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.client.logout()
        login = self.client.post(reverse('auth-login'), {'email': 'change@example.com', 'password': 'NewPass456!'})
        self.assertEqual(login.status_code, status.HTTP_200_OK)

    def test_change_password_wrong_old_password_rejected(self):
        self._login()
        response = self.client.post(self.url, {
            'old_password': 'WrongOld!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProfileViewTests(APITestCase):
    url = reverse('user-profile')

    def test_profile_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_get_profile_returns_expected_fields(self):
        user = make_user(email='profile@example.com')
        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['email'], 'profile@example.com')
        self.assertIn('is_verified', response.data)

    def test_patch_profile_updates_fields(self):
        user = make_user(email='profile2@example.com')
        self.client.force_authenticate(user=user)
        response = self.client.patch(self.url, {'first_name': 'Ada'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['first_name'], 'Ada')

    def test_patch_profile_duplicate_email_rejected(self):
        make_user(email='taken@example.com')
        user = make_user(email='profile3@example.com')
        self.client.force_authenticate(user=user)
        response = self.client.patch(self.url, {'email': 'taken@example.com'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ThrottlingTests(APITestCase):
    """Real throttle rates are widened to effectively-unlimited while testing
    (see settings.py) so the rest of the suite doesn't trip over shared IP-based
    throttle buckets - this test patches the 'login' rate directly on the throttle
    class to prove the LoginRateThrottle wiring actually works. (override_settings
    on DEFAULT_THROTTLE_RATES doesn't work here: SimpleRateThrottle.THROTTLE_RATES
    is bound to api_settings.DEFAULT_THROTTLE_RATES once at import time, not
    re-read per request.)"""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @patch.object(LoginRateThrottle, 'THROTTLE_RATES', {'login': '2/min'})
    def test_login_throttled_after_too_many_attempts(self):
        url = reverse('auth-login')
        payload = {'email': 'nobody@example.com', 'password': 'wrong'}

        first = self.client.post(url, payload)
        second = self.client.post(url, payload)
        third = self.client.post(url, payload)

        self.assertEqual(first.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(third.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn('detail', third.data)


class OtpHashingTests(TestCase):
    """OTP codes are stored as an HMAC-SHA256 keyed by a server-side secret,
    not a bare digest. With only 10^6 possible codes, a plain SHA-256 column is
    trivially reversible from a database dump alone - the pepper is what makes
    the dump insufficient on its own."""

    def test_stored_hash_is_not_a_bare_sha256_of_the_code(self):
        self.assertNotEqual(hash_otp_code('123456'), hashlib.sha256(b'123456').hexdigest())

    def test_hash_is_keyed_by_the_pepper(self):
        with override_settings(OTP_PEPPER_KEY='pepper-one'):
            first = hash_otp_code('123456')
        with override_settings(OTP_PEPPER_KEY='pepper-two'):
            second = hash_otp_code('123456')

        self.assertNotEqual(first, second)

    def test_pepper_falls_back_to_secret_key(self):
        with override_settings(OTP_PEPPER_KEY='', SECRET_KEY='key-one'):
            first = hash_otp_code('123456')
        with override_settings(OTP_PEPPER_KEY='', SECRET_KEY='key-two'):
            second = hash_otp_code('123456')

        self.assertNotEqual(first, second)

    def test_hash_still_fits_the_otp_code_hash_column(self):
        """HMAC-SHA256 is the same 64 hex characters SHA-256 was, which is why
        swapping the algorithm needed no migration on Profile.otp_code_hash."""
        max_length = Profile._meta.get_field('otp_code_hash').max_length
        self.assertLessEqual(len(hash_otp_code('123456')), max_length)

    def test_verification_round_trips_through_the_real_hash(self):
        user = make_user(email='hmac@example.com')
        user.is_active = False
        user.save(update_fields=['is_active'])
        code = issue_otp(user)

        self.assertEqual(verify_otp(user, code), (True, ''))


class OtpConcurrencyTests(TransactionTestCase):
    """The attempt counter is the only thing standing between an attacker and
    an unlimited number of guesses at a 6-digit code (the IP throttle is a
    backstop, not a defense against a distributed attempt - see docs/SECURITY.md
    §3.1). A read-modify-write on otp_attempts without a row lock loses
    increments under concurrency, which converts N parallel guesses into one
    counted attempt.

    TransactionTestCase rather than TestCase: the threads below need to see
    each other's committed writes, which the single wrapping transaction
    TestCase uses would hide.
    """

    def _user_with_otp(self):
        user = make_user(email='race@example.com')
        user.is_active = False
        user.save(update_fields=['is_active'])
        issue_otp(user)
        return user

    def test_verification_locks_the_profile_row(self):
        user = self._user_with_otp()

        with CaptureQueriesContext(connection) as queries:
            verify_otp(user, '000000')

        selects = [q['sql'] for q in queries.captured_queries if 'accounts_profile' in q['sql'].lower()]
        self.assertTrue(
            any('FOR UPDATE' in sql.upper() for sql in selects),
            f'expected a SELECT ... FOR UPDATE on the profile row, got: {selects}',
        )

    def test_concurrent_wrong_guesses_each_cost_an_attempt(self):
        user = self._user_with_otp()
        guesses = OTP_MAX_ATTEMPTS

        def guess():
            try:
                verify_otp(user, '000000')
            finally:
                # Each thread gets its own connection; leaving them open leaks
                # them past the test and blocks TransactionTestCase's teardown.
                connection.close()

        with ThreadPoolExecutor(max_workers=guesses) as pool:
            list(pool.map(lambda _: guess(), range(guesses)))

        user.profile.refresh_from_db()
        self.assertEqual(user.profile.otp_attempts, guesses)

    def test_lockout_holds_after_concurrent_guesses(self):
        """The point of counting every attempt: once the counter is spent, the
        real code stops working until a new one is issued."""
        user = self._user_with_otp()
        code = issue_otp(user)

        def guess():
            try:
                verify_otp(user, '000000')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=OTP_MAX_ATTEMPTS) as pool:
            list(pool.map(lambda _: guess(), range(OTP_MAX_ATTEMPTS)))

        self.assertEqual(verify_otp(user, code), (False, 'too_many_attempts'))
        user.refresh_from_db()
        self.assertFalse(user.is_active)


class PasswordChangeRevokesSessionsTests(APITestCase):
    """A password change is how a user boots an attacker (or a lost device) out
    of their account. That only means anything if the refresh tokens issued to
    those other sessions stop being accepted."""

    change_url = reverse('auth-change-password')
    refresh_url = reverse('auth-refresh')

    def _other_session_refresh_token(self, user):
        return str(RefreshToken.for_user(user))

    def test_change_password_revokes_other_sessions(self):
        user = make_user(email='revoke@example.com', password='OldPass123!')
        stolen = self._other_session_refresh_token(user)

        self.client.force_authenticate(user=user)
        response = self.client.post(self.change_url, {
            'old_password': 'OldPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        other = APIClient()
        other.cookies[REFRESH_COOKIE] = stolen
        self.assertEqual(other.post(self.refresh_url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_change_password_keeps_the_calling_session_working(self):
        """Revoking everything and issuing nothing back would log the caller out
        a few minutes later, once their access token expired - so the response
        carries a fresh pair."""
        user = make_user(email='revoke2@example.com', password='OldPass123!')
        self.client.force_authenticate(user=user)

        response = self.client.post(self.change_url, {
            'old_password': 'OldPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(ACCESS_COOKIE, response.cookies)
        self.assertIn(REFRESH_COOKIE, response.cookies)

        fresh = APIClient()
        fresh.cookies[REFRESH_COOKIE] = response.cookies[REFRESH_COOKIE].value
        self.assertEqual(fresh.post(self.refresh_url).status_code, status.HTTP_200_OK)

    def test_reset_password_revokes_all_sessions(self):
        user = make_user(email='revoke3@example.com', password='OldPass123!')
        stolen = self._other_session_refresh_token(user)

        response = self.client.post(reverse('auth-reset-password'), {
            'uid': urlsafe_base64_encode(force_bytes(user.pk)),
            'token': default_token_generator.make_token(user),
            'new_password': 'NewPass456!', 'new_password2': 'NewPass456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        other = APIClient()
        other.cookies[REFRESH_COOKIE] = stolen
        self.assertEqual(other.post(self.refresh_url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revocation_is_idempotent(self):
        """bulk_create(ignore_conflicts=True) rather than a plain insert: a
        concurrent logout can blacklist the same token in between."""
        user = make_user(email='revoke4@example.com')
        RefreshToken.for_user(user)

        self.assertEqual(revoke_all_refresh_tokens(user), 1)
        self.assertEqual(revoke_all_refresh_tokens(user), 0)


class PwnedPasswordValidatorTests(TestCase):
    """The HIBP check is switched off for the rest of the suite (it would put a
    live HTTPS call on every password set - see settings.PWNED_PASSWORDS_ENABLED),
    so this turns it back on and stubs the API client."""

    def _validator(self, hits):
        client = Mock()
        client.check_password.return_value = hits
        return PwnedPasswordsValidator(api_client=client)

    def test_configured_in_auth_password_validators(self):
        names = [validator['NAME'] for validator in settings.AUTH_PASSWORD_VALIDATORS]
        self.assertIn('accounts.password_validation.PwnedPasswordsValidator', names)

    @override_settings(PWNED_PASSWORDS_ENABLED=True)
    def test_breached_password_rejected(self):
        with self.assertRaises(DjangoValidationError) as ctx:
            self._validator(hits=42).validate('Str0ng-But-Breached!')

        self.assertEqual(ctx.exception.error_list[0].code, 'password_compromised')

    @override_settings(PWNED_PASSWORDS_ENABLED=True)
    def test_unbreached_password_accepted(self):
        self._validator(hits=0).validate('Str0ng-And-Unseen!')

    @override_settings(PWNED_PASSWORDS_ENABLED=True)
    def test_api_failure_falls_back_to_the_common_password_list(self):
        """Fail closed, not open: if HIBP is unreachable the password still has
        to clear Django's own common-password list rather than sail through."""
        client = Mock()
        client.check_password.side_effect = PwnedPasswordsError(
            'unreachable', code=PwnedPasswordsErrorCode.API_TIMEOUT, params={},
        )
        validator = PwnedPasswordsValidator(api_client=client)

        with self.assertRaises(DjangoValidationError):
            validator.validate('password')
        validator.validate('Str0ng-And-Unseen!')

    @override_settings(PWNED_PASSWORDS_ENABLED=False)
    def test_disabled_switch_skips_the_network_call_entirely(self):
        client = Mock()
        PwnedPasswordsValidator(api_client=client).validate('Str0ng-But-Breached!')

        client.check_password.assert_not_called()

    @override_settings(PWNED_PASSWORDS_ENABLED=True)
    @patch('pwned_passwords_django.api.default_client.check_password', return_value=99)
    def test_registration_rejects_a_breached_password(self, _mock_check):
        """End-to-end through the real serializer: RegisterSerializer runs
        Django's validate_password(), so the validator applies to registration,
        password change and password reset alike."""
        response = self.client.post(reverse('auth-register'), {
            'email': 'breached@example.com',
            'password': 'Str0ng-But-Breached!',
            'password2': 'Str0ng-But-Breached!',
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email='breached@example.com').exists())


class OtpVerifyThrottlingTests(APITestCase):
    """Both throttles on the OTP-check endpoint. Rates are widened to
    effectively-unlimited during the suite (see settings.py), so each test
    patches THROTTLE_RATES on the throttle class directly - the same technique
    ThrottlingTests uses, and for the same reason (SimpleRateThrottle binds
    THROTTLE_RATES at import, so override_settings on the rates has no effect).
    """

    url = reverse('auth-verify-email')

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _attempt(self, email='target@example.com', code='000000'):
        return self.client.post(self.url, {'email': email, 'code': code})

    @patch.object(OtpVerifyAccountRateThrottle, 'THROTTLE_RATES', {'otp_verify_account': '2/hour'})
    def test_account_throttle_limits_attempts_against_one_email(self):
        self.assertEqual(self._attempt().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._attempt().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._attempt().status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch.object(OtpVerifyAccountRateThrottle, 'THROTTLE_RATES', {'otp_verify_account': '2/hour'})
    def test_account_throttle_does_not_spill_onto_a_different_email(self):
        """The point of keying on the target: exhausting one account's bucket
        must not lock a second account out from the same IP."""
        self._attempt(email='first@example.com')
        self._attempt(email='first@example.com')
        self.assertEqual(self._attempt(email='first@example.com').status_code, status.HTTP_429_TOO_MANY_REQUESTS)

        self.assertEqual(self._attempt(email='second@example.com').status_code, status.HTTP_400_BAD_REQUEST)

    @patch.object(OtpVerifyAccountRateThrottle, 'THROTTLE_RATES', {'otp_verify_account': '2/hour'})
    def test_account_throttle_key_is_case_insensitive(self):
        """Buckets have to match the serializer's normalize_email(), or varying
        the capitalisation of the target address would reset the counter."""
        self._attempt(email='Target@Example.com')
        self._attempt(email='target@example.com')

        self.assertEqual(self._attempt(email='TARGET@EXAMPLE.COM').status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch.object(OtpVerifyRateThrottle, 'THROTTLE_RATES', {'otp_verify': '2/hour'})
    def test_ip_throttle_still_applies_across_different_emails(self):
        """The pre-existing IP-level backstop is unchanged: spreading attempts
        across many target accounts does not buy an attacker extra volume."""
        self.assertEqual(self._attempt(email='a@example.com').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._attempt(email='b@example.com').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._attempt(email='c@example.com').status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch.object(OtpVerifyAccountRateThrottle, 'THROTTLE_RATES', {'otp_verify_account': '1/hour'})
    def test_request_without_an_email_is_not_account_throttled(self):
        """Nothing to key on, so this throttle abstains - the request still
        fails validation, and the IP throttle still counts it."""
        first = self.client.post(self.url, {'code': '000000'})
        second = self.client.post(self.url, {'code': '000000'})

        self.assertEqual(first.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_per_code_lockout_is_unchanged_by_the_new_throttle(self):
        """The 5-attempt per-OTP lockout remains the primary defense; the
        throttles sit on top of it, not in place of it."""
        user = make_user(email='lockout@example.com')
        user.is_active = False
        user.save(update_fields=['is_active'])
        code = issue_otp(user)

        for _ in range(OTP_MAX_ATTEMPTS):
            self.assertEqual(self._attempt(email=user.email).status_code, status.HTTP_400_BAD_REQUEST)

        response = self._attempt(email=user.email, code=code)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Too many', response.data['code'][0])


class EmailChangeVerificationTests(APITestCase):
    """Changing the email address must not carry the verified flag across to an
    address nobody proved control of. `email` was writable while `is_verified`
    was read-only, so a PATCH silently kept a verified badge - and pointed
    future password-reset mail at the new address."""

    url = reverse('user-profile')

    def setUp(self):
        self.user = make_user(email='original@example.com', verified=True)
        self.client.force_authenticate(user=self.user)

    @patch('accounts.emails.BrevoClient')
    def test_changing_email_clears_verification_and_sends_a_code(self, mock_brevo_cls):
        response = self.client.patch(self.url, {'email': 'moved@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'moved@example.com')
        self.assertFalse(self.user.profile.is_verified)
        self.assertIsNotNone(self.user.profile.otp_code_hash)
        mock_brevo_cls.return_value.send_email.assert_called_once()

    @patch('accounts.emails.BrevoClient')
    def test_account_stays_usable_during_reverification(self, _mock_brevo_cls):
        """This costs the user a verification step, not access - is_active is
        untouched, so they can still log in and use the app."""
        self.client.patch(self.url, {'email': 'moved2@example.com'})

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    @patch('accounts.emails.BrevoClient')
    def test_new_address_can_be_verified_with_the_emailed_code(self, mock_brevo_cls):
        self.client.patch(self.url, {'email': 'moved3@example.com'})
        sent = mock_brevo_cls.return_value.send_email.call_args.kwargs['text_content']
        code = re.search(r'\b(\d{6})\b', sent).group(1)

        verify = APIClient().post(
            reverse('auth-verify-email'), {'email': 'moved3@example.com', 'code': code},
        )

        self.assertEqual(verify.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.profile.is_verified)

    @patch('accounts.emails.BrevoClient')
    def test_email_send_failure_rolls_back_the_change(self, mock_brevo_cls):
        """No half-applied state: the address must not move to one that has no
        code on its way to it."""
        mock_brevo_cls.return_value.send_email.side_effect = BrevoAPIError('boom')

        response = self.client.patch(self.url, {'email': 'never@example.com'})

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'original@example.com')
        self.assertTrue(self.user.profile.is_verified)

    @patch('accounts.emails.BrevoClient')
    def test_non_email_updates_do_not_trigger_reverification(self, mock_brevo_cls):
        response = self.client.patch(self.url, {'first_name': 'Ada'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.profile.is_verified)
        mock_brevo_cls.return_value.send_email.assert_not_called()

    @patch('accounts.emails.BrevoClient')
    def test_resubmitting_the_same_email_is_not_a_change(self, mock_brevo_cls):
        response = self.client.patch(self.url, {'email': 'original@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.profile.is_verified)
        mock_brevo_cls.return_value.send_email.assert_not_called()
