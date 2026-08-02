from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from analyses.models import Analysis

User = get_user_model()


def make_user(email='user@example.com', is_staff=False, is_active=True):
    user = User.objects.create_user(
        username=email.split('@')[0], email=email, password='TestPass123!',
        is_staff=is_staff, is_active=is_active,
    )
    return user


class AdminPermissionTests(APITestCase):
    """Every adminapi endpoint must be admin-only - a regular authenticated
    user must be rejected the same as an anonymous one, just with 403 instead
    of 401 (IsAdminUser vs no authentication at all)."""

    urls = [
        ('admin-users', {}),
        ('admin-analysis', {}),
        ('admin-stats', {}),
    ]

    def test_endpoints_reject_anonymous_users(self):
        for name, kwargs in self.urls:
            response = self.client.get(reverse(name, kwargs=kwargs))
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED, name)

    def test_endpoints_reject_non_staff_users(self):
        user = make_user(email='regular@example.com')
        self.client.force_authenticate(user=user)
        for name, kwargs in self.urls:
            response = self.client.get(reverse(name, kwargs=kwargs))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, name)

    def test_endpoints_allow_staff_users(self):
        admin = make_user(email='admin@example.com', is_staff=True)
        self.client.force_authenticate(user=admin)
        for name, kwargs in self.urls:
            response = self.client.get(reverse(name, kwargs=kwargs))
            self.assertEqual(response.status_code, status.HTTP_200_OK, name)

    def test_user_delete_rejects_non_staff(self):
        target = make_user(email='target@example.com')
        requester = make_user(email='nonstaff@example.com')
        self.client.force_authenticate(user=requester)
        response = self.client.delete(reverse('admin-user-delete', kwargs={'pk': target.pk}))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())


class AdminUserListViewTests(APITestCase):
    def setUp(self):
        self.admin = make_user(email='admin@example.com', is_staff=True)
        self.client.force_authenticate(user=self.admin)

    def test_lists_all_users_with_expected_fields(self):
        make_user(email='alice@example.com')
        response = self.client.get(reverse('admin-users'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data['count'], 2)
        row = next(r for r in response.data['results'] if r['email'] == 'alice@example.com')
        self.assertIn('is_verified', row)
        self.assertIn('analyses_count', row)
        self.assertIn('is_staff', row)

    def test_filters_by_query(self):
        make_user(email='findme@example.com')
        make_user(email='other@example.com')
        response = self.client.get(reverse('admin-users'), {'q': 'findme'})
        emails = [r['email'] for r in response.data['results']]
        self.assertIn('findme@example.com', emails)
        self.assertNotIn('other@example.com', emails)

    def test_filters_by_is_active(self):
        make_user(email='inactive@example.com', is_active=False)
        response = self.client.get(reverse('admin-users'), {'is_active': 'false'})
        emails = [r['email'] for r in response.data['results']]
        self.assertIn('inactive@example.com', emails)
        self.assertNotIn(self.admin.email, emails)


class AdminUserDeleteViewTests(APITestCase):
    def setUp(self):
        self.admin = make_user(email='admin@example.com', is_staff=True)
        self.client.force_authenticate(user=self.admin)

    def test_admin_can_delete_another_user(self):
        target = make_user(email='target@example.com')
        response = self.client.delete(reverse('admin-user-delete', kwargs={'pk': target.pk}))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=target.pk).exists())

    def test_admin_cannot_delete_own_account_via_this_endpoint(self):
        response = self.client.delete(reverse('admin-user-delete', kwargs={'pk': self.admin.pk}))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_deleting_nonexistent_user_404s(self):
        response = self.client.delete(reverse('admin-user-delete', kwargs={'pk': 999999}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AdminAnalysisListViewTests(APITestCase):
    def setUp(self):
        self.admin = make_user(email='admin@example.com', is_staff=True)
        self.client.force_authenticate(user=self.admin)
        self.owner = make_user(email='owner@example.com')

    def test_lists_analyses_across_all_owners(self):
        Analysis.objects.create(owner=self.owner, name='a.py', language='Python', status=Analysis.Status.COMPLETED)
        response = self.client.get(reverse('admin-analysis'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['owner_email'], 'owner@example.com')

    def test_filters_by_status_and_language(self):
        Analysis.objects.create(owner=self.owner, name='a.py', language='Python', status=Analysis.Status.COMPLETED)
        Analysis.objects.create(owner=self.owner, name='b.js', language='JavaScript', status=Analysis.Status.FAILED)

        response = self.client.get(reverse('admin-analysis'), {'status': Analysis.Status.COMPLETED})
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['language'], 'Python')

        response = self.client.get(reverse('admin-analysis'), {'language': 'javascript'})
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['status'], Analysis.Status.FAILED)


class AdminStatsViewTests(APITestCase):
    def test_returns_user_and_analysis_totals(self):
        admin = make_user(email='admin@example.com', is_staff=True)
        owner = make_user(email='owner@example.com')
        Analysis.objects.create(owner=owner, name='a.py', language='Python', status=Analysis.Status.COMPLETED)

        self.client.force_authenticate(user=admin)
        response = self.client.get(reverse('admin-stats'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['users']['total'], 2)
        self.assertEqual(response.data['analyses']['total'], 1)
        self.assertEqual(response.data['analyses']['completed'], 1)
