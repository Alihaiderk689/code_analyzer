from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.signing import dumps as signing_dumps
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.throttling import LoginRateThrottle
from github_integration.services.oauth_service import _STATE_SALT

from .brevo_client import BrevoAPIError
from .cookies import ACCESS_COOKIE, REFRESH_COOKIE
from .google_auth import GoogleTokenError, verify_google_access_token
from .otp import OTP_MAX_ATTEMPTS, issue_otp

User = get_user_model()


def make_user(email='user@example.com', password='TestPass123!', verified=False):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=password)
    user.profile.is_verified = verified
    user.profile.save(update_fields=['is_verified'])
    return user


class RegisterTests(APITestCase):
    url = reverse('auth-register')

    @patch('accounts.emails.BrevoClient')
    def test_register_creates_inactive_user_and_sends_otp_email(self, mock_brevo_cls):
        response = self.client.post(self.url, {
            'email': 'new@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='new@example.com')
        self.assertFalse(user.is_active)
        self.assertFalse(user.profile.is_verified)
        self.assertIsNotNone(user.profile.otp_code_hash)
        mock_brevo_cls.return_value.send_email.assert_called_once()
        self.assertEqual(mock_brevo_cls.return_value.send_email.call_args.kwargs['to_email'], 'new@example.com')

    @patch('accounts.emails.BrevoClient')
    def test_register_rolls_back_when_email_send_fails(self, mock_brevo_cls):
        mock_brevo_cls.return_value.send_email.side_effect = BrevoAPIError('boom')
        response = self.client.post(self.url, {
            'email': 'failed@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(User.objects.filter(email='failed@example.com').exists())

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
        self.assertEqual(csrf_response.status_code, status.HTTP_204_NO_CONTENT)
        csrf_token = client.cookies['csrftoken'].value

        response = client.post(
            reverse('auth-change-password'),
            {'old_password': 'TestPass123!', 'new_password': 'NewPass456!', 'new_password2': 'NewPass456!'},
            HTTP_X_CSRFTOKEN=csrf_token,
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
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        csrf_cookie = response.cookies['csrftoken']
        self.assertTrue(csrf_cookie.value)
        # Must be JS-readable (not httponly) - the frontend reads it via
        # document.cookie to echo it back as X-CSRFToken.
        self.assertFalse(csrf_cookie['httponly'])


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
