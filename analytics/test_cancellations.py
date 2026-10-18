import sys
from unittest.mock import Mock, patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from analytics.models import CancellationRecord
from Delivery.models import DeliveryDriver, DeliveryRequest, DeliveryOffer
from RideVTC.models import Ride
from Delivery import test_orders as loaded
from Delivery import views as delivery_views
# Reuse the suite's canonical views; its optional Firebase import is mocked.
sys.modules.setdefault('Delivery.views', delivery_views)
sys.modules.setdefault('Delivery.services.dispatch', loaded.dispatch_module)
DeliveryRequestViewSet = loaded.DeliveryRequestViewSet
DeliveryOfferViewSet = loaded.DeliveryOfferViewSet
with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
    from RideVTC.views import RideViewSet


class CancellationTests(TestCase):
    def setUp(self):
        worker = patch('analytics.detection_tasks.schedule_detection')
        worker.start(); self.addCleanup(worker.stop)
        User=get_user_model()
        self.client=User.objects.create(email='client@cancel.test')
        self.partner=User.objects.create(email='partner@cancel.test')
        self.other=User.objects.create(email='other@cancel.test')
        self.courier=DeliveryDriver.objects.create(user=self.partner,full_name='Courier',phone='1',is_available=False)
        self.factory=APIRequestFactory()
        self.reason={'cancellation_reason':'partner_requested','cancellation_note':'Private account','cancellation_version':1}

    def ride(self,status='accepted',mode='classic'):
        return Ride.objects.create(user=self.client,driver=self.partner,status=status,ride_mode=mode,pickup_location='A',dropoff_location='B',distance_km=1,price=1000)

    def delivery(self,status='assigned',**kw):
        return DeliveryRequest.objects.create(user=self.client,assigned_driver=self.courier,status=status,pickup_label='A',dropoff_label='B',contact_name='C',contact_phone='1',**kw)

    def cancel(self,obj,payload=None,user=None):
        req=self.factory.post('/cancel/',self.reason if payload is None else payload,format='json')
        force_authenticate(req,user=user or self.client)
        view=RideViewSet if isinstance(obj,Ride) else DeliveryRequestViewSet
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}), patch('RideVTC.views.channel_layer',None),patch('Delivery.views.publish_delivery_status'):
            return view.as_view({'post':'cancel'})(req,pk=obj.pk)

    def test_private_reason_and_idempotence(self):
        for obj in [self.ride(),self.delivery()]:
            response=self.cancel(obj)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('Private account',str(response.data))
            self.cancel(obj,{'cancellation_reason':'other'})
            event=obj.cancellation_record
            self.assertEqual(event.reason,'partner_requested')
            self.assertEqual(event.note,'Private account')
            self.assertEqual(event.status_before,'accepted' if isinstance(obj,Ride) else 'assigned')
        self.assertEqual(CancellationRecord.objects.count(),2)
        self.courier.refresh_from_db();self.assertTrue(self.courier.is_available)

    def test_invalid_reason_does_not_cancel(self):
        for payload in [{'cancellation_version':1},{'cancellation_reason':'fake'},{'cancellation_reason':'other','cancellation_note':'a'*501}]:
            for obj in [self.ride(),self.delivery()]:
                self.assertEqual(self.cancel(obj,payload).status_code,400)
                obj.refresh_from_db();self.assertNotEqual(obj.status,'cancelled')
        self.assertEqual(CancellationRecord.objects.count(),0)

    def test_old_clients_remain_cancellable_and_are_explicitly_identified(self):
        obj=self.ride();self.assertEqual(self.cancel(obj,{}).status_code,200)
        self.assertEqual(obj.cancellation_record.reason,'legacy_unspecified')

    @override_settings(CANCELLATION_REASON_REQUIRED=True)
    def test_strict_rollout_setting_rejects_missing_customer_reason(self):
        obj=self.ride();self.assertEqual(self.cancel(obj,{}).status_code,400)

    def test_other_user_cannot_cancel_or_write_reason(self):
        for obj in [self.ride(),self.delivery()]:
            self.assertIn(self.cancel(obj,user=self.other).status_code,[403,404])
        self.assertEqual(CancellationRecord.objects.count(),0)

    def test_started_and_financially_engaged_services_are_protected(self):
        objects=[self.ride(status='in_progress'),self.delivery(status='picked_up'),self.delivery(is_paid=True),self.delivery(purchase_funds_received_at=timezone.now()),self.delivery(purchase_advance_accepted_at=timezone.now())]
        for obj in objects:
            self.assertEqual(self.cancel(obj).status_code,409)
            obj.refresh_from_db();self.assertNotEqual(obj.status,'cancelled')
        self.assertEqual(CancellationRecord.objects.count(),0)

    def test_automatic_search_expiration_cannot_cancel_an_accepted_service(self):
        for obj in [self.ride(),self.delivery()]:
            self.assertEqual(self.cancel(obj,{'cancellation_reason':'search_timeout'}).status_code,400)

    def test_shared_cancellation_records_reason_and_checks_actual_status(self):
        obj=self.ride(mode='shared');self.assertEqual(self.cancel(obj).status_code,200)
        self.assertEqual(obj.cancellation_record.reason,'partner_requested')
        obj=self.ride(mode='shared');self.assertEqual(self.cancel(obj,{'cancellation_reason':'search_timeout'}).status_code,400)

    def test_cancelled_offer_is_no_longer_acceptable(self):
        from datetime import timedelta
        obj=self.delivery(status='searching');obj.assigned_driver=None;obj.save()
        offer=DeliveryOffer.objects.create(request=obj,driver=self.courier,distance_km=1,expires_at=timezone.now()+timedelta(minutes=1))
        self.assertEqual(self.cancel(obj).status_code,200)
        req=self.factory.post('/accept/',{},format='json');force_authenticate(req,user=self.partner)
        response=DeliveryOfferViewSet.as_view({'post':'accept'})(req,pk=offer.pk)
        self.assertIn(response.status_code,[400,409])
        obj.refresh_from_db();self.assertEqual(obj.status,'cancelled')

    def test_partner_notification_contains_status_but_not_private_report(self):
        obj=self.delivery()
        with patch.object(loaded.dispatch_module,'_ws_send_group') as send, patch('Delivery.views.publish_delivery_status'):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.cancel(obj).status_code,200)
        send.assert_called_once()
        group,event,payload=send.call_args.args
        self.assertEqual(group,f'delivery.driver.{self.partner.pk}')
        self.assertEqual(payload['status'],'cancelled')
        self.assertNotIn('Private account',str(payload))
        self.assertNotIn('partner_requested',str(payload))

    def test_paid_vtc_is_not_cancelled_without_financial_resolution(self):
        from RideVTC.models import Payment
        obj=self.ride()
        Payment.objects.create(ride=obj,amount=1000,wallet='CASH',provider='CASH',status='SUCCESS',idempotency_key='paid-before-cancel')
        self.assertEqual(self.cancel(obj).status_code,409)
        obj.refresh_from_db();self.assertEqual(obj.status,'accepted')

    def test_courier_is_not_released_if_another_delivery_is_active(self):
        obj=self.delivery()
        self.delivery(status='picked_up')
        self.assertEqual(self.cancel(obj).status_code,200)
        self.courier.refresh_from_db();self.assertFalse(self.courier.is_available)
