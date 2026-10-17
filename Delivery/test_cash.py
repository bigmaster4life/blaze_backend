from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
import time
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from users.models import CustomUser
from .models import DeliveryRequest, DeliveryDriver, DeliveryOffer
from . import test_orders as loaded
from . import views as loaded_views
DeliveryDriverViewSet = loaded_views.DeliveryDriverViewSet
DeliveryRequestViewSet = loaded.DeliveryRequestViewSet
DeliveryOfferViewSet = loaded.DeliveryOfferViewSet


class DeliveryCashTests(TestCase):
    def setUp(self):
        self.client_user = CustomUser.objects.create(email='cash-client@test.invalid')
        self.driver_user = CustomUser.objects.create(email='cash-driver@test.invalid')
        self.driver = DeliveryDriver.objects.create(user=self.driver_user, full_name='Livreur', phone='123', is_verified=True, onboarding_completed=True)
        self.req = DeliveryRequest.objects.create(user=self.client_user, assigned_driver=self.driver, status='assigned', type='package', vehicle_type='moto', pickup_zone='libreville', dropoff_zone='libreville', pickup_label='Retrait', dropoff_label='Destination', pickup_lat=.4, pickup_lng=9.4, dropoff_lat=.41, dropoff_lng=9.41, contact_name='Client', contact_phone='123', estimated_price=1500, final_price=1500, payment_payer='recipient')
        self.payload = {'type':'package','vehicle_type':'moto','pickup_zone':'libreville','dropoff_zone':'libreville','pickup_label':'Retrait','dropoff_label':'Destination','pickup_lat':.4,'pickup_lng':9.4,'dropoff_lat':.41,'dropoff_lng':9.41,'contact_name':'Client','contact_phone':'123','payment_method':'cash','payment_payer':'recipient'}

    def action(self, name, data=None, user=None):
        request = APIRequestFactory().post('/', data or {}, format='json')
        force_authenticate(request, user or self.driver_user)
        with patch('Delivery.views.publish_delivery_status'):
            return DeliveryDriverViewSet.as_view({'post':name})(request, pk=self.req.pk)

    def as_client(self, action, data, user=None):
        request = APIRequestFactory().post('/', data, format='json')
        force_authenticate(request, user or self.client_user)
        with patch('Delivery.views.publish_delivery_status'), patch('Delivery.views.create_offers_for_request', return_value=0):
            return DeliveryRequestViewSet.as_view({'post':action})(request)

    def buy(self, mode='client_funded', payer='recipient'):
        self.req.type='food'
        self.req.payment_payer=payer
        self.req.order_details={'kind':'shopping','merchant_name':'Magasin','items':'2 articles','purchase_mode':'buy_for_me','funding_mode':mode,'budget':5000}
        if mode=='driver_advance': self.req.purchase_advance_accepted_at=timezone.now()
        self.req.save()

    def test_delivery_completion_never_implies_payment_and_retries_do_not_duplicate_stats(self):
        self.req.status='picked_up'; self.req.save()
        self.assertEqual(self.action('mark_delivered').status_code,200)
        self.assertEqual(self.action('mark_delivered').status_code,200)
        self.req.refresh_from_db(); self.driver.refresh_from_db()
        self.assertFalse(self.req.is_paid); self.assertIsNone(self.req.cash_received_at)
        self.assertEqual(self.driver.total_deliveries,1); self.assertEqual(self.driver.total_earnings,0)

    def test_cash_confirmation_is_explicit_exact_and_idempotent(self):
        self.req.status='picked_up'; self.req.save()
        for body in ({'amount':1500}, {'amount':1500,'received':False}, {'amount':1,'received':True}):
            self.assertEqual(self.action('confirm_cash',body).status_code,400)
        for _ in range(2): self.assertEqual(self.action('confirm_cash',{'amount':1500,'received':True}).status_code,200)
        self.req.refresh_from_db(); self.driver.refresh_from_db()
        self.assertTrue(self.req.is_paid); self.assertIsNotNone(self.req.cash_received_at)
        self.assertEqual(self.driver.total_earnings,1170)

    def test_sender_payment_required_before_pickup_and_recipient_cannot_be_collected_early(self):
        self.assertEqual(self.action('confirm_cash',{'amount':1500,'received':True}).status_code,400)
        self.req.payment_payer='sender'; self.req.save()
        self.assertEqual(self.action('mark_picked_up').status_code,400)
        self.assertEqual(self.action('confirm_cash',{'amount':1500,'received':True}).status_code,200)
        self.assertEqual(self.action('mark_picked_up').status_code,200)

    def test_another_courier_cannot_confirm_cash(self):
        user=CustomUser.objects.create(email='other-courier@test.invalid')
        DeliveryDriver.objects.create(user=user,full_name='Autre',phone='456',is_verified=True,onboarding_completed=True)
        self.assertEqual(self.action('confirm_cash',{'amount':1500,'received':True},user).status_code,404)
        self.req.refresh_from_db(); self.assertFalse(self.req.is_paid)

    def test_unpaid_delivered_request_remains_accessible_for_collection(self):
        self.req.status='delivered'; self.req.save()
        request=APIRequestFactory().get('/'); force_authenticate(request,self.driver_user)
        response=DeliveryDriverViewSet.as_view({'get':'active_requests'})(request)
        self.assertEqual([row['id'] for row in response.data],[self.req.pk])

    def test_client_funded_purchase_requires_funds_and_change_return(self):
        self.buy()
        self.assertEqual(self.action('purchase_amount',{'amount':4200}).status_code,400)
        self.assertEqual(self.action('purchase_funds',{'amount':6000,'received':True}).status_code,400)
        self.assertEqual(self.action('purchase_funds',{'amount':5000,'received':True}).status_code,200)
        self.assertEqual(self.action('purchase_amount',{'amount':6000}).status_code,400)
        self.assertEqual(self.action('purchase_amount',{'amount':4200}).status_code,200)
        self.assertEqual(self.action('mark_picked_up').status_code,200)
        self.assertEqual(self.action('mark_delivered').status_code,400)
        self.assertEqual(self.action('mark_delivered',{'change_returned':True}).status_code,200)
        self.req.refresh_from_db(); self.driver.refresh_from_db()
        self.assertIsNotNone(self.req.purchase_change_returned_at)
        self.assertFalse(self.req.is_paid); self.assertEqual(self.driver.total_earnings,0)

    def test_advance_reimbursement_is_separate_from_delivery_fee(self):
        self.buy('driver_advance')
        self.assertEqual(self.action('purchase_amount',{'amount':4200}).status_code,200)
        self.assertEqual(self.action('purchase_reimbursement',{'amount':4200,'received':True}).status_code,400)
        self.assertEqual(self.action('mark_picked_up').status_code,200)
        for _ in range(2): self.assertEqual(self.action('purchase_reimbursement',{'amount':4200,'received':True}).status_code,200)
        self.req.refresh_from_db(); self.driver.refresh_from_db()
        self.assertIsNotNone(self.req.purchase_reimbursed_at)
        self.assertFalse(self.req.is_paid); self.assertEqual(self.driver.total_earnings,0)
        self.assertEqual(self.action('confirm_cash',{'amount':1500,'received':True}).status_code,200)
        self.driver.refresh_from_db(); self.assertEqual(self.driver.total_earnings,1170)

    def test_advance_requires_explicit_agreement_before_acceptance(self):
        self.buy('driver_advance'); self.req.purchase_advance_accepted_at=None
        self.req.assigned_driver=None; self.req.status='searching'; self.req.save()
        offer=DeliveryOffer.objects.create(request=self.req,driver=self.driver,expires_at=timezone.now()+timedelta(minutes=5))
        for body, expected in [({},400),({'accept_purchase_advance':True},200)]:
            request=APIRequestFactory().post('/',body,format='json'); force_authenticate(request,self.driver_user)
            response=DeliveryOfferViewSet.as_view({'post':'accept'})(request,pk=offer.pk)
            self.assertEqual(response.status_code,expected)
        self.req.refresh_from_db(); self.assertIsNotNone(self.req.purchase_advance_accepted_at)

    def test_quote_is_read_only_and_uses_server_distance(self):
        before=DeliveryRequest.objects.count()
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':12000}):
            response=self.as_client('quote',{**self.payload,'distance_km':0})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['delivery_price'],2000)
        self.assertEqual(DeliveryRequest.objects.count(),before)
        created=self.as_client('create',{**self.payload,'distance_km':0,'estimated_price':1,'quote_token':response.data['quote_token']})
        self.assertEqual(created.status_code,201)
        self.assertEqual(created.data['estimated_price'],2000)
        self.assertEqual(created.data['distance_km'],12)

    def test_quote_rejects_changed_route_other_customer_and_expiry(self):
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':1000}):
            quote=self.as_client('quote',self.payload).data['quote_token']
        changed=self.as_client('create',{**self.payload,'dropoff_lat':0.5,'quote_token':quote})
        self.assertEqual(changed.status_code,400)
        other=CustomUser.objects.create(email='other-client@test.invalid')
        self.assertEqual(self.as_client('create',{**self.payload,'quote_token':quote},other).status_code,400)
        now=time.time()
        with patch('django.core.signing.time.time',return_value=now+301):
            self.assertEqual(self.as_client('create',{**self.payload,'quote_token':quote}).status_code,400)
        self.assertEqual(self.as_client('create',self.payload).status_code,400)

    def test_airtel_is_not_accepted_until_integration_exists(self):
        self.assertEqual(self.as_client('quote',{**self.payload,'payment_method':'airtel'}).status_code,400)
        self.assertEqual(self.as_client('create',{**self.payload,'payment_method':'airtel'}).status_code,400)

    def test_sender_advance_requires_both_delivery_fee_and_purchase_reimbursement_before_departure(self):
        self.buy('driver_advance','sender')
        self.assertEqual(self.action('purchase_amount',{'amount':4200}).status_code,200)
        self.assertEqual(self.action('mark_picked_up').status_code,400)
        self.assertEqual(self.action('confirm_cash',{'amount':1500,'received':True}).status_code,200)
        self.assertEqual(self.action('mark_picked_up').status_code,400)
        self.assertEqual(self.action('purchase_reimbursement',{'amount':4200,'received':True}).status_code,200)
        self.assertEqual(self.action('purchase_amount',{'amount':4500}).status_code,400)
        self.assertEqual(self.action('mark_picked_up').status_code,200)

    def test_completed_delivery_cannot_expose_the_courier_location(self):
        self.req.status='delivered'; self.req.save()
        request=APIRequestFactory().get('/');force_authenticate(request,self.client_user)
        response=DeliveryRequestViewSet.as_view({'get':'driver_location'})(request,pk=self.req.pk)
        self.assertEqual(response.status_code,400)

    def test_fee_allocation_excludes_goods_and_is_recorded_only_once(self):
        from .models import DeliveryPaymentAllocation
        from .services.accounting import driver_summary
        self.buy('driver_advance')
        self.action('purchase_amount', {'amount':4200})
        self.action('mark_picked_up')
        self.action('purchase_reimbursement', {'amount':4200,'received':True})
        for _ in range(2): self.action('confirm_cash', {'amount':1500,'received':True})
        self.assertEqual(DeliveryPaymentAllocation.objects.count(),1)
        allocation = self.req.payment_allocation
        self.assertEqual(allocation.paid_amount,1500)
        self.assertEqual(allocation.platform_amount,330)
        self.assertEqual(allocation.driver_amount,1170)
        self.assertEqual(allocation.cash_commission_due,330)
        summary = driver_summary(self.driver)
        self.assertEqual(Decimal(summary['net']),1170)
        self.assertIsNone(summary['withdrawable_balance'])

    def test_new_order_defaults_to_sender_without_rewriting_old_orders(self):
        payload = dict(self.payload)
        payload.pop('payment_payer')
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':1000}):
            quote = self.as_client('quote',payload)
        response = self.as_client('create',{**payload,'quote_token':quote.data['quote_token']})
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.data['payment_payer'],'sender')
        self.req.refresh_from_db()
        self.assertEqual(self.req.payment_payer,'recipient')

    def test_allocation_rounding_preserves_total_and_retries_do_not_rewrite_it(self):
        from .models import DeliveryPaymentAllocation
        self.req.status='picked_up'; self.req.final_price=1525; self.req.save()
        self.assertEqual(self.action('confirm_cash', {'amount':1525,'received':True}).status_code,200)
        allocation=self.req.payment_allocation
        self.assertEqual(allocation.platform_amount,336)
        self.assertEqual(allocation.driver_amount,1189)
        self.assertEqual(allocation.platform_amount+allocation.driver_amount,1525)
        DeliveryRequest.objects.filter(pk=self.req.pk).update(final_price=9999)
        self.action('confirm_cash', {'amount':9999,'received':True})
        allocation.refresh_from_db(); self.assertEqual(allocation.paid_amount,1525)
        self.assertEqual(DeliveryPaymentAllocation.objects.count(),1)

    def test_historical_paid_fee_does_not_become_a_new_net_earning(self):
        from .models import DeliveryPaymentAllocation
        self.req.status='delivered'; self.req.is_paid=True; self.req.save()
        self.driver.total_earnings=9000; self.driver.save()
        self.assertEqual(self.action('confirm_cash', {'amount':1500,'received':True}).status_code,200)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.total_earnings,9000)
        self.assertFalse(DeliveryPaymentAllocation.objects.exists())

    def test_profile_stats_and_admin_expose_same_read_only_accounting(self):
        from .serializers import DeliveryDriverProfileSerializer, DeliveryDriverStatsSerializer, DeliveryDriverAdminDetailSerializer
        self.req.status='picked_up'; self.req.save()
        self.action('confirm_cash', {'amount':1500,'received':True})
        for serializer_class in (DeliveryDriverProfileSerializer, DeliveryDriverStatsSerializer, DeliveryDriverAdminDetailSerializer):
            summary=serializer_class(self.driver).data['settlement_summary']
            self.assertEqual(Decimal(summary['net']),1170)
            self.assertEqual(Decimal(summary['commission']),330)
            self.assertEqual(Decimal(summary['cash_commission_due']),330)
            self.assertIsNone(summary['withdrawable_balance'])
        serializer=DeliveryDriverProfileSerializer(self.driver,data={'total_earnings':999999,'total_deliveries':999},partial=True)
        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data,{})
