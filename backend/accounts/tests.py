from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.throttling import LoginRateThrottle

from .cookies import ACCESS_COOKIE, REFRESH_COOKIE
from .tokens import email_verification_token

User = get_user_model()


def make_user(email='user@example.com', password='TestPass123!', verified=False):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=password)
    user.profile.is_verified = verified
    user.profile.save(update_fields=['is_verified'])
    return user


class RegisterTests(APITestCase):
    url = reverse('auth-register')

    def test_register_creates_user_and_sends_verification_email(self):
        response = self.client.post(self.url, {
            'email': 'new@example.com', 'password': 'TestPass123!', 'password2': 'TestPass123!',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='new@example.com')
        self.assertFalse(user.profile.is_verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('verify-email', mail.outbox[0].body)

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

    def test_forgot_password_existing_user_sends_email(self):
        make_user(email='forgot@example.com')
        response = self.client.post(self.url, {'email': 'forgot@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('reset-password', mail.outbox[0].body)

    def test_forgot_password_nonexistent_user_same_response(self):
        response = self.client.post(self.url, {'email': 'ghost@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['detail'], 'If an account with that email exists, a reset link has been sent.')
        self.assertEqual(len(mail.outbox), 0)


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

    def _link_params(self, user):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = email_verification_token.make_token(user)
        return uid, token

    def test_verify_email_valid_token_marks_verified(self):
        user = make_user(email='verify@example.com')
        uid, token = self._link_params(user)

        response = self.client.get(self.url, {'uid': uid, 'token': token})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.profile.refresh_from_db()
        self.assertTrue(user.profile.is_verified)

    def test_verify_email_missing_params_400(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_email_invalid_token_400(self):
        user = make_user(email='verify2@example.com')
        uid, _ = self._link_params(user)
        response = self.client.get(self.url, {'uid': uid, 'token': 'garbage'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResendVerificationTests(APITestCase):
    url = reverse('auth-resend-verification')

    def test_resend_for_unverified_user_sends_email(self):
        make_user(email='unverified@example.com', verified=False)
        response = self.client.post(self.url, {'email': 'unverified@example.com'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_for_already_verified_user_sends_nothing(self):
        make_user(email='verified@example.com', verified=True)
        response = self.client.post(self.url, {'email': 'verified@example.com'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)


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
