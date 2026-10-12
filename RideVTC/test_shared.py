from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate
from users.models import CustomUser
from drivers.models import Driver
from .models import Ride, RideVehicle, RidePricing, SharedBooking, Payment
from .shared import accept_first, try_join, complete_stop, cancel_booking, record_position, capacity_ok, match_waiting
from .shared_views import SharedTripView, SharedCurrentView
from .pricing import QuoteInput, make_quote
from .serializers import RideCreateSerializer

@override_settings(VTC_SHARED_ENABLED=True, VTC_SHARED_MAX_DETOUR_SECONDS=300)
class SharedTests(TestCase):
    def setUp(self):
        self.driver = CustomUser.objects.create(email='driver@tests.com', user_type='driver')
        Driver.objects.create(user=self.driver, full_name='Test', phone='123', vehicle_plate='TEST')
        self.a = CustomUser.objects.create(email='a@tests.com')
        self.b = CustomUser.objects.create(email='b@tests.com')
        self.vehicle = RideVehicle.objects.create(driver=self.driver, brand='Test', model='Test', city='Test',
            latitude=0, longitude=0, shared_enabled=True, passenger_capacity=4)
        self.ride = self.make_ride(self.a, 0, .01)
        self.trip = accept_first(self.ride.pk, self.driver)
        self.trip.last_location_at = timezone.now()
        self.trip.save()

    def make_ride(self, user, pickup, dropoff, passengers=1):
        ride = Ride.objects.create(user=user, ride_mode='shared', passenger_count=passengers, category='eco',
            pickup_lat=0, pickup_lng=pickup, dropoff_lat=0, dropoff_lng=dropoff,
            pickup_location='Pickup', dropoff_location='Dropoff', price=1000, distance_km=1)
        RidePricing.objects.create(ride=ride, version='classic-v1', reference_price=1000,
            commission_rate='.22', quoted_distance_km=1, quoted_duration_seconds=1000)
        return ride

    def stop(self):
        self.trip.refresh_from_db()
        first = self.trip.stops[0]
        self.trip.last_lat, self.trip.last_lng = first['lat'], first['lng']
        self.trip.last_location_at = timezone.now()
        self.trip.save()
        self.trip = complete_stop(self.trip.pk, self.driver, self.trip.version, first['ride_id'], first['kind'])

    def join(self, passengers=1):
        other = self.make_ride(self.b, .002, .005, passengers)
        with patch('RideVTC.shared.road_legs', side_effect=lambda origin, stops: [100]*len(stops)):
            joined = try_join(other)
        self.trip.refresh_from_db()
        return other, joined

    def test_shorter_second_ride_dropped_before_first(self):
        self.stop()
        other, joined = self.join()
        self.assertTrue(joined)
        self.assertEqual([(s['ride_id'],s['kind']) for s in self.trip.stops],
            [(other.pk,'pickup'),(other.pk,'dropoff'),(self.ride.pk,'dropoff')])
        other.refresh_from_db()
        self.assertEqual(other.driver_id, self.driver.pk)

    def test_capacity_rejects_too_many_passengers(self):
        self.stop()
        other, joined = self.join(passengers=4)
        self.assertFalse(joined)
        other.refresh_from_db()
        self.assertEqual(other.status, 'pending')

    def test_no_driver_consent_no_join(self):
        self.vehicle.shared_enabled = False
        self.vehicle.save()
        _, joined = self.join()
        self.assertFalse(joined)

    def test_provider_failure_leaves_separate_pending_request(self):
        other = self.make_ride(self.b, .002, .005)
        with patch('RideVTC.shared.road_legs', return_value=None):
            self.assertFalse(try_join(other))
        self.assertFalse(SharedBooking.objects.filter(ride=other).exists())

    def test_large_detour_rejected(self):
        other = self.make_ride(self.b, .002, .005)
        with patch('RideVTC.shared.road_legs', side_effect=lambda origin, stops: [100]*len(stops) if len(stops)==2 else [1000]*len(stops)):
            self.assertFalse(try_join(other))

    def test_cancel_only_one_client_and_preserve_other(self):
        other, joined = self.join()
        self.assertTrue(joined)
        cancel_booking(other.pk, self.b)
        self.trip.refresh_from_db()
        self.assertEqual(len(self.trip.stops),2)
        self.assertTrue(all(s['ride_id']==self.ride.pk for s in self.trip.stops))
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.status,'accepted')
        with self.assertRaises(PermissionDenied): cancel_booking(self.ride.pk,self.b)

    def test_cancelled_offer_cannot_be_joined(self):
        other = self.make_ride(self.b,.002,.005)
        other.status='cancelled';other.save()
        with patch('RideVTC.shared.road_legs', side_effect=lambda origin, stops: [100]*len(stops)):
            self.assertFalse(try_join(other))

    def test_stale_stop_and_wrong_order_rejected(self):
        with self.assertRaises(ValidationError): complete_stop(self.trip.pk,self.driver,0,self.ride.pk,'pickup')
        with self.assertRaises(ValidationError): complete_stop(self.trip.pk,self.driver,self.trip.version,self.ride.pk,'dropoff')
        self.assertFalse(Payment.objects.exists())

    def test_gps_required_before_boarding(self):
        self.trip.last_lng=1;self.trip.save()
        with self.assertRaises(ValidationError): complete_stop(self.trip.pk,self.driver,self.trip.version,self.ride.pk,'pickup')

    def test_onboard_client_cannot_be_cancelled(self):
        self.stop()
        with self.assertRaises(ValidationError): cancel_booking(self.ride.pk,self.a)

    def test_measured_shared_segment_updates_both_bookings(self):
        self.stop()
        other, joined=self.join()
        self.assertTrue(joined)
        self.stop()
        self.trip.last_location_at=timezone.now()-timedelta(seconds=10)
        self.trip.save()
        record_position(self.trip,0,.003)
        for booking in self.trip.bookings.all():
            self.assertGreater(booking.shared_m,100)
            self.assertEqual(booking.shared_m,booking.travelled_m)

    def test_half_shared_distance_discount_and_22_percent_allocation(self):
        self.stop()
        SharedBooking.objects.filter(ride=self.ride).update(travelled_m=2000,shared_m=1000)
        self.stop()
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.final_price,Decimal(900))
        payment=Payment.objects.get(ride=self.ride)
        self.assertEqual(payment.allocation.platform_amount,198)
        self.assertEqual(payment.allocation.driver_amount,702)
        self.assertEqual(payment.allocation.cash_commission_due,198)
        self.assertEqual(self.trip.state,'completed')
        with self.assertRaises(ValidationError): complete_stop(self.trip.pk,self.driver,self.trip.version,self.ride.pk,'dropoff')
        self.assertEqual(Payment.objects.count(),1)

    def test_no_shared_distance_no_discount(self):
        self.stop();self.stop()
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.final_price,1000)

    def test_customer_cannot_read_other_clients_stops(self):
        request=APIRequestFactory().get('/')
        force_authenticate(request,self.a)
        self.assertEqual(SharedTripView.as_view()(request,pk=self.trip.pk).status_code,404)
        request=APIRequestFactory().get('/')
        force_authenticate(request,self.driver)
        self.assertEqual(SharedCurrentView.as_view()(request).data['id'],self.trip.pk)

    def test_shared_quote_disabled_and_signed_passengers_bound(self):
        data=dict(pickup_lat=0,pickup_lng=0,dropoff_lat=0,dropoff_lng=.01,category='eco',ride_mode='shared',passenger_count=2)
        with override_settings(VTC_SHARED_ENABLED=False):
            self.assertFalse(QuoteInput(data=data).is_valid())
        quote=make_quote(self.a.pk,data,metrics=(1,1000))
        request=APIRequestFactory().post('/');request.user=self.a
        payload={**data,'pickup_location':'A','dropoff_location':'B','quote_token':quote['quote_token'],'passenger_count':1}
        serializer=RideCreateSerializer(data=payload,context={'request':request})
        self.assertFalse(serializer.is_valid())

    def test_initial_capacity_checked_before_first_dropoff(self):
        self.ride.status='in_progress';self.ride.passenger_count=5
        self.assertFalse(capacity_ok([self.trip.stops[-1]],{self.ride.pk:self.ride},4))

    def test_existing_second_request_matched_after_first_gps(self):
        other=self.make_ride(self.b,.002,.005)
        with patch('RideVTC.shared.road_legs', side_effect=lambda origin, stops: [100]*len(stops)):
            match_waiting(self.trip.pk)
        self.assertTrue(SharedBooking.objects.filter(ride=other).exists())

    def test_old_request_not_revived(self):
        other=self.make_ride(self.b,.002,.005)
        Ride.objects.filter(pk=other.pk).update(requested_at=timezone.now()-timedelta(minutes=2))
        other.refresh_from_db()
        self.assertFalse(try_join(other))
        with self.assertRaises(ValidationError): accept_first(other.pk,self.driver)

    def test_pending_cancellation_prevents_acceptance(self):
        other=self.make_ride(self.b,.002,.005)
        cancel_booking(other.pk,self.b)
        other.refresh_from_db()
        self.assertEqual(other.status,'cancelled')
        self.assertFalse(try_join(other))

    def test_b_payment_does_not_finish_a_and_discounts_capped(self):
        self.stop()
        other,joined=self.join()
        self.assertTrue(joined)
        self.stop()
        SharedBooking.objects.filter(ride=other).update(travelled_m=1000,shared_m=1500)
        self.stop()
        other.refresh_from_db();self.ride.refresh_from_db()
        self.assertEqual(other.final_price,800)
        self.assertEqual(self.ride.status,'in_progress')
        self.assertEqual(self.trip.state,'active')
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(Payment.objects.get(ride=other).allocation.driver_amount,624)
        self.stop()
        self.assertEqual(Payment.objects.count(),2)
        self.assertEqual(self.trip.state,'completed')

    def test_impossible_gps_jump_does_not_create_discount(self):
        self.stop()
        self.trip.last_location_at=timezone.now()-timedelta(seconds=1)
        self.trip.save()
        record_position(self.trip,0,1)
        self.assertEqual(self.trip.bookings.get().travelled_m,0)

    def test_vehicle_settings_locked_during_shared_trip(self):
        import sys
        from unittest.mock import Mock
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
            from .views import DriverVehicleMe
        request=APIRequestFactory().put('/',{'passenger_capacity':1},format='json')
        force_authenticate(request,self.driver)
        self.assertEqual(DriverVehicleMe.as_view()(request).status_code,409)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.passenger_capacity,4)

    def test_live_hides_other_booking_from_customer(self):
        import sys
        from unittest.mock import Mock
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
            from .views import RideViewSet
        self.stop();other,joined=self.join()
        self.assertTrue(joined)
        request=APIRequestFactory().get('/')
        force_authenticate(request,self.a)
        response=RideViewSet.as_view({'get':'live'})(request,pk=self.ride.pk)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('shared_trip',response.data)
        request=APIRequestFactory().get('/')
        force_authenticate(request,self.driver)
        response=RideViewSet.as_view({'get':'live'})(request,pk=self.ride.pk)
        self.assertEqual(len(response.data['shared_trip']['bookings']),2)

    def test_cancellation_retries_membership_added_while_waiting(self):
        from unittest.mock import Mock
        other,joined=self.join()
        self.assertTrue(joined)
        member=SharedBooking.objects.get(ride=other)
        query=Mock();query.first.side_effect=[None,member];query.exists.return_value=True
        with patch.object(SharedBooking.objects,'filter',return_value=query):
            cancel_booking(other.pk,self.b)
        self.trip.refresh_from_db();other.refresh_from_db()
        self.assertEqual(other.status,'cancelled')
        self.assertTrue(all(stop['ride_id']!=other.pk for stop in self.trip.stops))

    def test_creation_returns_accepted_shared_booking_even_without_socket(self):
        import sys
        from unittest.mock import Mock
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
            from .views import RideViewSet
        self.stop()
        data=dict(pickup_lat=0,pickup_lng=.002,dropoff_lat=0,dropoff_lng=.005,category='eco',ride_mode='shared',passenger_count=1)
        quote=make_quote(self.b.pk,data,metrics=(1,1000))
        request=APIRequestFactory().post('/',{**data,'pickup_location':'B pickup','dropoff_location':'B destination',
            'distance_km':1,'price':1000,'quote_token':quote['quote_token']},format='json')
        force_authenticate(request,self.b)
        with patch('RideVTC.shared.road_legs',side_effect=lambda origin, stops:[100]*len(stops)):
            response=RideViewSet.as_view({'post':'create'})(request)
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.data['status'],'accepted')
        self.assertEqual(response.data['ride_mode'],'shared')
        self.assertTrue(SharedBooking.objects.filter(ride_id=response.data['id']).exists())

    def test_repeated_creation_reuses_accepted_booking(self):
        import sys
        from unittest.mock import Mock
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
            from .views import RideViewSet
        self.stop()
        data=dict(pickup_lat=0,pickup_lng=0,dropoff_lat=0,dropoff_lng=.01,category='eco',ride_mode='shared',passenger_count=1)
        quote=make_quote(self.a.pk,data,metrics=(1,1000))
        request=APIRequestFactory().post('/',{**data,'pickup_location':'A','dropoff_location':'End',
            'distance_km':1,'price':1000,'quote_token':quote['quote_token']},format='json')
        force_authenticate(request,self.a)
        response=RideViewSet.as_view({'post':'create'})(request)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['id'],self.ride.pk)
        self.assertEqual(Ride.objects.count(),1)

    def test_legacy_signed_classic_quote_cannot_become_shared(self):
        from django.core import signing
        from .pricing import SALT, read_quote
        data=dict(pickup_lat=0,pickup_lng=0,dropoff_lat=0,dropoff_lng=.01,category='eco')
        quote=make_quote(self.a.pk,data,metrics=(1,1000))
        payload=signing.loads(quote['quote_token'],salt=SALT)
        payload.pop('ride_mode');payload.pop('passenger_count')
        token=signing.dumps(payload,salt=SALT)
        self.assertEqual(read_quote(token,self.a.pk,data)['price'],'1000')
        with self.assertRaises(ValidationError): read_quote(token,self.a.pk,{**data,'ride_mode':'shared'})

    def test_history_records_discount_and_only_own_reservation(self):
        from .history import RideHistoryView
        self.stop()
        SharedBooking.objects.filter(ride=self.ride).update(travelled_m=1000,shared_m=1000)
        self.stop()
        request=APIRequestFactory().get('/',{'role':'customer'})
        force_authenticate(request,self.a)
        response=RideHistoryView.as_view()(request)
        self.assertEqual(len(response.data['results']),1)
        row=response.data['results'][0]
        self.assertEqual(row['shared_discount_percent'],20)
        self.assertEqual(row['ride_mode'],'shared')
        self.assertEqual(Decimal(row['final_price']),800)
