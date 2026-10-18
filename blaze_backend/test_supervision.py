from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate
from drivers.models import Driver
from RideVTC.models import DriverPresence, Ride
from Delivery.models import DeliveryDriver, DeliveryRequest
from .supervision import SupervisionView, position, city_key
from RideVTC.supervision_presence import record_position


class SupervisionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(first_name='Test',last_name='User',email='manager@example.test', password='test', user_type='manager_staff', is_staff=True)
        self.client_user = User.objects.create_user(first_name='Test',last_name='User',email='client@example.test', password='test',user_type='client')
        self.driver_user = User.objects.create_user(first_name='Test',last_name='User',email='driver@example.test', password='test', user_type='chauffeur')
        self.driver = Driver.objects.create(user=self.driver_user, full_name='Partner', phone='123', vehicle_plate='AA', onboarding_completed=True, status='approved')
        self.factory = APIRequestFactory()

    def snapshot(self, user):
        request = self.factory.get('/api/supervision/')
        if user:
            force_authenticate(request, user)
        return SupervisionView.as_view()(request)

    def test_permissions(self):
        User=get_user_model()
        employee=User.objects.create_user(first_name='Test',last_name='User',email='employee@example.test',password='test',user_type='employee_staff',is_staff=True)
        for user in [None, self.client_user, self.driver_user, employee]:
            self.assertIn(self.snapshot(user).status_code, [401,403])
        self.assertEqual(self.snapshot(self.manager).status_code,200)
        self.manager.is_active=False;self.manager.save()
        self.assertEqual(self.snapshot(self.manager).status_code,403)

    def test_heartbeat_does_not_make_old_gps_fresh(self):
        now=timezone.now()
        DriverPresence.objects.create(driver=self.driver_user, is_online=True, last_seen=now, lat=0.4,lng=9.4, location_updated_at=now-timedelta(minutes=5),city='Libreville')
        result=self.snapshot(self.manager)
        p=result.data['partners'][0]
        self.assertTrue(p['online']);self.assertFalse(p['position']['fresh'])
        self.assertEqual(p['country'],'GA')
        self.assertEqual(result['Cache-Control'],'no-store, private')

    def test_missing_gps_stays_unknown(self):
        DriverPresence.objects.create(driver=self.driver_user,is_online=True,last_seen=timezone.now(),lat=0.4,lng=9.4,city='Unknown city')
        p=self.snapshot(self.manager).data['partners'][0]
        self.assertFalse(p['position']['fresh']);self.assertEqual(p['country'],'unknown')

    def test_gps_validation(self):
        self.assertFalse(record_position(self.driver_user.pk,float('nan'),9))
        self.assertFalse(record_position(self.driver_user.pk,91,9))
        self.assertTrue(record_position(self.driver_user.pk,0,0))
        self.assertTrue(position(0,0,timezone.now(),timezone.now())['fresh'])
        self.assertEqual(city_key('Port-Gentil'),'port_gentil')
        self.assertEqual(city_key('Angondjé'),'angondje')

    def test_active_rides_and_pending_requests_without_customer_identity(self):
        Ride.objects.create(user=self.client_user,driver=self.driver_user,status='in_progress',pickup_location='A',dropoff_location='B',distance_km=1,price=1000,pickup_city='Port-Gentil')
        Ride.objects.create(user=self.client_user,status='pending',pickup_location='A',dropoff_location='C',distance_km=1,price=1000)
        Ride.objects.create(user=self.client_user,status='completed',pickup_location='A',dropoff_location='D',distance_km=1,price=1000)
        data=self.snapshot(self.manager).data
        self.assertEqual(len(data['jobs']),2)
        self.assertEqual(data['partners'][0]['state'],'busy')
        self.assertEqual(data['partners'][0]['country'],'GA')
        self.assertNotIn('user',data['jobs'][0]);self.assertNotIn('phone',data['jobs'][0])

    def test_delivery_pickup_and_fresh_available(self):
        courier=DeliveryDriver.objects.create(user=self.client_user,full_name='Courier',phone='12',city='port_gentil',is_verified=True,last_lat=-0.7,last_lng=8.7,last_location_update=timezone.now())
        p=[p for p in self.snapshot(self.manager).data['partners'] if p['service']=='delivery'][0]
        self.assertEqual(p['state'],'available')
        order=DeliveryRequest.objects.create(user=self.driver_user,assigned_driver=courier,status='assigned',pickup_label='Shop',dropoff_label='Home',contact_name='Customer',contact_phone='123')
        data=self.snapshot(self.manager).data
        self.assertEqual([p for p in data['partners'] if p['service']=='delivery'][0]['state'],'pickup')
        order.status='picked_up';order.save()
        self.assertEqual([p for p in self.snapshot(self.manager).data['partners'] if p['service']=='delivery'][0]['state'],'busy')

    def test_delivery_location_records_timestamp_and_rejects_invalid_coordinates(self):
        import sys
        from unittest.mock import Mock, patch
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
            from Delivery.views import DeliveryDriverLocationViewSet
        courier = DeliveryDriver.objects.create(user=self.client_user, full_name='Courier', phone='12')
        view = DeliveryDriverLocationViewSet.as_view({'post': 'update_location'})
        for payload, expected in [({'lat':'nan','lng':9},400), ({'lat':100,'lng':9},400), ({'lat':0.4,'lng':9.4},200)]:
            request = self.factory.post('/location/', payload, format='json')
            force_authenticate(request,self.client_user)
            self.assertEqual(view(request).status_code,expected)
        courier.refresh_from_db()
        self.assertIsNotNone(courier.last_location_update)
        self.assertEqual(courier.last_lat,0.4)


from django.test import SimpleTestCase
from unittest.mock import AsyncMock
from asgiref.sync import async_to_sync


class GPSWebsocketTests(SimpleTestCase):
    def test_location_handler_awaits_already_async_database_method(self):
        from RideVTC.consumers import DriverConsumer
        consumer=DriverConsumer()
        consumer.user_id=123
        consumer._update_driver_location=AsyncMock()
        async_to_sync(consumer.receive_json)({'type':'location','lat':0.4,'lng':9.4})
        consumer._update_driver_location.assert_awaited_once_with(0.4,9.4)
