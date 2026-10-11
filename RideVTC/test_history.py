from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from users.models import CustomUser
from .history import RideHistoryView
from .models import Ride


class RideHistoryTests(TestCase):
    def setUp(self):
        self.customer = CustomUser.objects.create(email='history-client@example.com', user_type='client')
        self.driver = CustomUser.objects.create(email='history-driver@example.com', user_type='driver')
        self.other = CustomUser.objects.create(email='history-other@example.com', user_type='client')
        self.ride = self.make_ride(self.customer, self.driver, 'completed')
        self.make_ride(self.other, None, 'completed')
        self.make_ride(self.customer, self.driver, 'in_progress')

    def make_ride(self, customer, driver, status):
        return Ride.objects.create(user=customer, driver=driver, status=status,
                                   pickup_location='Départ', dropoff_location='Arrivée',
                                   distance_km=2, price=1500, final_price=1750)

    def request(self, user=None, **params):
        request = APIRequestFactory().get('/api/rides/history/', params)
        if user:
            force_authenticate(request, user=user)
        return RideHistoryView.as_view()(request)

    def test_anonymous_denied(self):
        self.assertIn(self.request().status_code, (401, 403))

    def test_customer_only_sees_own_past_rides(self):
        response = self.request(self.customer)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['id'] for r in response.data['results']], [self.ride.pk])
        self.assertEqual(response.data['results'][0]['final_price'], '1750.00')
        self.assertNotIn('user', response.data['results'][0])

    def test_driver_only_sees_assigned_past_rides(self):
        self.assertEqual([r['id'] for r in self.request(self.driver, role='driver').data['results']], [self.ride.pk])
        self.assertEqual(self.request(self.other, role='driver').data['results'], [])

    def test_role_cannot_select_another_user(self):
        response = self.request(self.other, user_id=self.customer.pk)
        self.assertNotIn(self.ride.pk, [r['id'] for r in response.data['results']])
        self.assertEqual(self.request(self.customer, role='admin').status_code, 400)

    def test_cancelled_and_rejected_are_visible(self):
        cancelled = self.make_ride(self.customer, self.driver, 'cancelled')
        rejected = self.make_ride(self.customer, self.driver, 'rejected')
        ids = [r['id'] for r in self.request(self.customer).data['results']]
        self.assertEqual(ids, [rejected.pk, cancelled.pk, self.ride.pk])

    def test_pagination_preserves_all_rides(self):
        for _ in range(21):
            self.make_ride(self.customer, self.driver, 'completed')
        first = self.request(self.customer).data
        second = self.request(self.customer, page=2).data
        self.assertEqual(first['count'], 22)
        self.assertEqual(len(first['results']), 20)
        self.assertEqual(len(second['results']), 2)
        self.assertIsNotNone(first['next'])
        self.assertIsNone(second['next'])
        self.assertFalse({r['id'] for r in first['results']} & {r['id'] for r in second['results']})
