from datetime import timedelta
from unittest.mock import Mock, patch
import sys
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from users.models import CustomUser
from .models import DeliveryDriver, DeliveryOffer, DeliveryRequest
from .serializers import DeliveryRequestCreateSerializer, DeliveryOfferOutSerializer
with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
    from .views import DeliveryOfferViewSet, DeliveryRequestViewSet
    from .services.dispatch import _broadcast_offer_created, create_offers_for_request
    import importlib
    dispatch_module = importlib.import_module("Delivery.services.dispatch")

DETAILS = {'kind':'restaurant','merchant_name':'Le Palmier','items':'2 poulets braisés, 1 riz',
           'purchase_mode':'prepaid','reference':'BE-123'}

class DeliveryOrderTests(TestCase):
    def setUp(self):
        self.user=CustomUser.objects.create(email='buyer@order.test')
        self.driver_user=CustomUser.objects.create(email='courier@order.test')
        self.driver=DeliveryDriver.objects.create(user=self.driver_user,full_name='Livreur',phone='123',
            is_verified=True,onboarding_completed=True,last_lat=.4,last_lng=9.4)
        self.payload={'type':'food','vehicle_type':'moto','pickup_zone':'libreville','dropoff_zone':'libreville',
            'pickup_label':'Restaurant adresse','pickup_lat':.4,'pickup_lng':9.4,
            'dropoff_label':'Client adresse','dropoff_lat':.41,'dropoff_lng':9.41,
            'distance_km':1,'contact_name':'Client','contact_phone':'123'}

    def api_payload(self, payload):
        from django.core import signing
        from .services.quotes import fingerprint, SALT
        return {**payload, 'quote_token': signing.dumps({'user_id': self.user.pk, 'route': fingerprint(payload), 'distance_km': 1, 'price': None if payload.get('vehicle_type') == 'van' else 1500}, salt=SALT)}

    def serializer(self,details=None,**overrides):
        payload={**self.payload,**overrides}
        if details is not None: payload['order_details']=details
        request=APIRequestFactory().post('/');request.user=self.user
        return DeliveryRequestCreateSerializer(data=payload,context={'request':request})

    def make_request(self,details=DETAILS):
        serializer=self.serializer(details)
        self.assertTrue(serializer.is_valid(),serializer.errors)
        return serializer.save()

    def test_prepaid_reference_survives_creation_and_offer(self):
        delivery=self.make_request()
        offer=DeliveryOffer.objects.create(request=delivery,driver=self.driver,expires_at=timezone.now()+timedelta(minutes=5))
        data=DeliveryOfferOutSerializer(offer).data
        self.assertEqual(data['order_details']['reference'],'BE-123')
        self.assertEqual(data['order_details']['items'],DETAILS['items'])
        self.assertEqual(data['pickup_label'],'Restaurant adresse')
        self.assertEqual(data['dropoff_lat'],.41)
        self.assertIsNone(data['order_details']['budget'])

    def test_shopping_budget_is_distinct_from_delivery_price(self):
        delivery=self.make_request({**DETAILS,'kind':'shopping','merchant_name':'CKDO','purchase_mode':'buy_for_me','budget':5000})
        delivery.refresh_from_db()
        self.assertEqual(delivery.order_details['budget'],5000)
        self.assertEqual(delivery.estimated_price,1500)
        self.assertFalse(delivery.is_paid)

    def test_missing_items_merchant_or_budget_rejected(self):
        for changed in ({'items':' '},{'merchant_name':' '},{'purchase_mode':'buy_for_me'},
                        {'purchase_mode':'buy_for_me','budget':0},{'purchase_mode':'buy_for_me','budget':1000001},
                        {'kind':'unknown'},{'purchase_mode':'invalid'}):
            with self.subTest(changed=changed):
                self.assertFalse(self.serializer({**DETAILS,**changed}).is_valid())

    def test_prepaid_cannot_retain_a_purchase_budget(self):
        delivery=self.make_request({**DETAILS,'budget':5000})
        self.assertIsNone(delivery.order_details['budget'])

    def test_legacy_food_without_details_and_package_remain_valid(self):
        for category in ['food','package','documents','express']:
            serializer=self.serializer(type=category)
            self.assertTrue(serializer.is_valid(),serializer.errors)
            self.assertEqual(serializer.save().order_details,{})

    def test_non_food_cannot_include_food_details(self):
        self.assertFalse(self.serializer(DETAILS,type='package').is_valid())

    def test_offer_is_sent_only_to_its_driver_with_matching_ids(self):
        delivery=self.make_request()
        offer=DeliveryOffer.objects.create(request=delivery,driver=self.driver,expires_at=timezone.now()+timedelta(minutes=5))
        with patch.object(dispatch_module, '_ws_send_group') as send:
            _broadcast_offer_created(offer)
        send.assert_called_once()
        group,event,payload=send.call_args.args
        self.assertEqual(group,f'delivery.driver.{self.driver_user.pk}')
        self.assertEqual(event,'delivery.offer.new')
        self.assertEqual(payload['offerId'],offer.pk)
        self.assertEqual(payload['requestId'],delivery.pk)

    def test_blocked_or_unverified_driver_cannot_accept(self):
        delivery=self.make_request()
        offer=DeliveryOffer.objects.create(request=delivery,driver=self.driver,expires_at=timezone.now()+timedelta(minutes=5))
        for field in ['is_blocked','is_verified','onboarding_completed']:
            self.driver.is_blocked=field=='is_blocked'
            self.driver.is_verified=field!='is_verified'
            self.driver.onboarding_completed=field!='onboarding_completed'
            self.driver.save()
            request=APIRequestFactory().post('/');force_authenticate(request,self.driver_user)
            response=DeliveryOfferViewSet.as_view({'post':'accept'})(request,pk=offer.pk)
            self.assertEqual(response.status_code,403)
            delivery.refresh_from_db()
            self.assertIsNone(delivery.assigned_driver_id)

    def test_dispatch_excludes_unverified_courier(self):
        delivery=self.make_request()
        self.driver.is_verified=False;self.driver.save()
        self.assertEqual(create_offers_for_request(delivery),0)
        self.assertFalse(DeliveryOffer.objects.exists())

    def test_dispatch_calls_push_and_websocket_for_the_same_saved_order(self):
        delivery=self.make_request()
        with patch.object(dispatch_module,'notify_delivery_driver_new_order',return_value=True) as push, patch.object(dispatch_module,'_ws_send_group') as websocket:
            self.assertEqual(create_offers_for_request(delivery),1)
        push.assert_called_once()
        courier,order=push.call_args.args
        self.assertEqual(courier.pk,self.driver.pk)
        self.assertEqual(order.order_details['merchant_name'],'Le Palmier')
        self.assertEqual(websocket.call_count,1)
        self.assertEqual(websocket.call_args.args[2]['requestId'],delivery.pk)

    def test_create_endpoint_returns_saved_request_for_every_delivery_type(self):
        for category in ['food', 'package', 'documents', 'express']:
            with self.subTest(category=category):
                request = APIRequestFactory().post('/', self.api_payload({**self.payload, 'type': category}), format='json')
                force_authenticate(request, self.user)
                with patch('Delivery.views.create_offers_for_request', return_value=0), patch('Delivery.views.notify_client_update'):
                    response = DeliveryRequestViewSet.as_view({'post': 'create'})(request)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.data['order_details'], {})
                self.assertEqual(response.data['status'], 'no_driver')
                self.assertEqual(DeliveryRequest.objects.filter(pk=response.data['id']).count(), 1)

    def test_create_endpoint_preserves_food_details_and_rejects_incomplete_details(self):
        request = APIRequestFactory().post('/', self.api_payload({**self.payload, 'order_details': DETAILS}), format='json')
        force_authenticate(request, self.user)
        with patch('Delivery.views.create_offers_for_request', return_value=1):
            response = DeliveryRequestViewSet.as_view({'post': 'create'})(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['order_details']['kind'], 'restaurant')
        self.assertEqual(response.data['order_details']['reference'], 'BE-123')
        self.assertEqual(response.data['status'], 'searching')
        before = DeliveryRequest.objects.count()
        request = APIRequestFactory().post('/', self.api_payload({**self.payload, 'order_details': {}}), format='json')
        force_authenticate(request, self.user)
        response = DeliveryRequestViewSet.as_view({'post': 'create'})(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DeliveryRequest.objects.count(), before)

    def dispatch_silently(self, delivery):
        with patch.object(dispatch_module, 'notify_delivery_driver_new_order'), patch.object(dispatch_module, '_ws_send_group'):
            return create_offers_for_request(delivery)

    def test_nearby_courier_from_another_city_receives_offer(self):
        delivery = self.make_request()
        delivery.city = 'angondje'
        delivery.save()
        self.assertEqual(self.driver.city, 'libreville')
        self.assertEqual(self.dispatch_silently(delivery), 1)
        self.assertEqual(delivery.offers.get().driver_id, self.driver.pk)

    def test_faraway_courier_in_same_city_receives_no_offer(self):
        delivery = self.make_request()
        self.driver.last_lat = -0.7
        self.driver.last_lng = 8.8
        self.driver.save()
        self.assertEqual(self.dispatch_silently(delivery), 0)

    def test_unknown_or_invalid_driver_position_never_uses_city_as_fallback(self):
        delivery = self.make_request()
        for latitude, longitude in [(None, None), (91, 9), (0.4, 181)]:
            self.driver.last_lat, self.driver.last_lng = latitude, longitude
            self.driver.save()
            self.assertEqual(self.dispatch_silently(delivery), 0)
        self.assertFalse(delivery.offers.exists())

    def test_dispatch_preserves_vehicle_and_verification_requirements(self):
        delivery = self.make_request()
        self.driver.vehicle_type = 'car'
        self.driver.save()
        self.assertEqual(self.dispatch_silently(delivery), 0)

    def test_dispatch_expands_radius_and_does_not_send_duplicate_offers(self):
        delivery = self.make_request()
        self.driver.last_lat = 0.57  # about 19 km away: beyond first radius
        self.driver.save()
        self.assertEqual(self.dispatch_silently(delivery), 1)
        self.assertGreater(delivery.offers.get().distance_km, 15)
        self.assertEqual(self.dispatch_silently(delivery), 0)
        self.assertEqual(delivery.offers.count(), 1)

    def test_van_alone_requires_operator_and_never_dispatches(self):
        request = APIRequestFactory().post('/', self.api_payload({**self.payload, 'vehicle_type': 'van', 'van_service': 'moving'}), format='json')
        force_authenticate(request, self.user)
        with patch('Delivery.views.create_offers_for_request') as dispatch:
            response = DeliveryRequestViewSet.as_view({'post': 'create'})(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'operator_required')
        dispatch.assert_not_called()

    def test_expired_offers_report_no_driver_instead_of_manual_treatment(self):
        delivery = self.make_request()
        DeliveryOffer.objects.create(request=delivery, driver=self.driver, expires_at=timezone.now()-timedelta(seconds=1))
        with patch.object(dispatch_module, '_ws_send_group'):
            dispatch_module.expire_offers_and_request_if_needed(delivery)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'no_driver')

    def test_real_create_dispatches_motorcycle_to_nearby_driver_across_zone_border(self):
        payload = {**self.payload, 'pickup_zone': 'angondje'}
        request = APIRequestFactory().post('/', self.api_payload(payload), format='json')
        force_authenticate(request, self.user)
        with patch.object(dispatch_module, 'notify_delivery_driver_new_order'), patch.object(dispatch_module, '_ws_send_group'):
            response = DeliveryRequestViewSet.as_view({'post': 'create'})(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'searching')
        self.assertEqual(DeliveryOffer.objects.get(request_id=response.data['id']).driver_id, self.driver.pk)

    def test_car_without_matching_driver_is_not_sent_to_operator(self):
        request = APIRequestFactory().post('/', self.api_payload({**self.payload, 'vehicle_type': 'car'}), format='json')
        force_authenticate(request, self.user)
        with patch('Delivery.views.notify_client_update'):
            response = DeliveryRequestViewSet.as_view({'post': 'create'})(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'no_driver')
        self.assertFalse(DeliveryOffer.objects.filter(request_id=response.data['id']).exists())

    def test_accept_publishes_committed_status_without_waiting_for_firebase(self):
        delivery = self.make_request()
        offer = DeliveryOffer.objects.create(request=delivery, driver=self.driver, expires_at=timezone.now()+timedelta(minutes=5))
        request = APIRequestFactory().post('/')
        force_authenticate(request, self.driver_user)
        with patch.object(dispatch_module, '_ws_send_group') as websocket, patch('notifications.delivery_tasks.submit_delivery_status') as submit:
            with self.captureOnCommitCallbacks(execute=True):
                response = DeliveryOfferViewSet.as_view({'post': 'accept'})(request, pk=offer.pk)
                self.assertEqual(response.status_code, 200)
                websocket.assert_not_called()
            self.assertTrue(any(call.args[0] == f'user.{self.user.pk}' and call.args[1] == 'delivery.request.update' and call.args[2]['status'] == 'assigned' for call in websocket.call_args_list))
            submit.assert_called_once_with(delivery.pk, 'assigned')
        delivery.refresh_from_db()
        self.assertEqual(delivery.assigned_driver_id, self.driver.pk)

    def test_offer_websocket_is_sent_before_firebase_network_call(self):
        delivery = self.make_request()
        events = []
        with patch.object(dispatch_module, '_ws_send_group', side_effect=lambda *a: events.append('ws')), patch.object(dispatch_module, 'notify_delivery_driver_new_order', side_effect=lambda *a: events.append('push')):
            self.assertEqual(create_offers_for_request(delivery), 1)
        self.assertEqual(events, ['ws', 'push'])
