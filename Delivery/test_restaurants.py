import uuid
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory,force_authenticate
from users.models import CustomUser
from .models import Restaurant,RestaurantMenuItem,RestaurantOrder,RestaurantOrderEvent,DeliveryRequest,DeliveryDriver,CustomerReceipt
from .serializers import DeliveryRequestCreateSerializer,DeliveryRequestOutSerializer
from .restaurant_api import RestaurantCatalog,RestaurantAdmin,MenuAdmin,OperatorOrders
from .receipts import ReceiptView,ReceiptList
import sys
from . import test_orders as loaded
from . import views as loaded_views
sys.modules.setdefault('Delivery.views',loaded_views)
sys.modules.setdefault('Delivery.services.dispatch',loaded.dispatch_module)
DeliveryRequestViewSet=loaded.DeliveryRequestViewSet
DeliveryDriverViewSet=loaded_views.DeliveryDriverViewSet


class RestaurantTests(TestCase):
    def setUp(self):
        self.user=CustomUser.objects.create(email='restaurant-client@test.invalid')
        self.staff=CustomUser.objects.create(email='restaurant-manager@test.invalid',is_staff=True,user_type='manager_staff')
        self.other=CustomUser.objects.create(email='restaurant-other@test.invalid')
        self.driver_user=CustomUser.objects.create(email='restaurant-courier@test.invalid')
        self.driver=DeliveryDriver.objects.create(user=self.driver_user,full_name='Livreur',phone='123',is_verified=True,onboarding_completed=True)
        self.restaurant=Restaurant.objects.create(name='Restaurant test',city='port_gentil',address='Retrait restaurant',latitude=-.72,longitude=8.78,is_active=True,is_open=True)
        self.dish=RestaurantMenuItem.objects.create(restaurant=self.restaurant,name='Plat test',price=2500)
        self.payload={'type':'food','vehicle_type':'moto','restaurant_id':self.restaurant.pk,'cart':[{'item_id':self.dish.pk,'quantity':2}],'checkout_id':str(uuid.uuid4()),'pickup_zone':'libreville','dropoff_zone':'port_gentil','pickup_label':'Faux retrait','pickup_lat':.4,'pickup_lng':9.4,'dropoff_label':'Adresse client','dropoff_lat':-.73,'dropoff_lng':8.79,'contact_name':'Client','contact_phone':'123'}

    def call(self,view,action,data=None,user=None,pk=None,method='post'):
        request=getattr(APIRequestFactory(),method)('/',data or {},format='json');force_authenticate(request,user or self.user)
        return view.as_view({method:action})(request,**({'pk':pk} if pk else {}))

    def checkout(self,payload=None):
        payload=payload or self.payload
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':1500}):
            quote=self.call(DeliveryRequestViewSet,'quote',payload)
        self.assertEqual(quote.status_code,200,quote.data)
        with patch('Delivery.views.create_offers_for_request') as dispatch:
            response=self.call(DeliveryRequestViewSet,'create',{**payload,'quote_token':quote.data['quote_token']})
            dispatch.assert_not_called()
        self.assertEqual(response.status_code,201,response.data)
        return DeliveryRequest.objects.get(pk=response.data['id']),quote

    def operator(self,order,action,**extra):
        with patch('Delivery.restaurant_api.dispatch_restaurant'):
            return self.call(OperatorOrders,'advance',{'action':action,'version':order.version,**extra},self.staff,order.pk)

    def test_checkout_quotes_canonical_menu_and_waits_for_operator(self):
        req,quote=self.checkout()
        self.assertEqual(quote.data['food_total'],5000)
        self.assertEqual(quote.data['total'],5000+req.estimated_price)
        self.assertEqual(req.pickup_label,self.restaurant.address)
        self.assertEqual(req.pickup_lat,self.restaurant.latitude)
        self.assertEqual(req.payment_payer,'recipient')
        self.assertEqual(req.status,'operator_required')
        self.assertTrue(req.order_details['managed_by_be'])
        self.assertNotIn('funding',DeliveryRequestOutSerializer(req).data['restaurant_order'])

    def test_idempotent_checkout_retry_does_not_create_another_request(self):
        a,_=self.checkout();b,_=self.checkout()
        self.assertEqual(a.pk,b.pk)
        self.assertEqual(RestaurantOrder.objects.count(),1)
        changed={**self.payload,'dropoff_label':'Autre adresse'}
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':1500}):
            quote=self.call(DeliveryRequestViewSet,'quote',changed)
        response=self.call(DeliveryRequestViewSet,'create',{**changed,'quote_token':quote.data['quote_token']})
        self.assertEqual(response.status_code,400)

    def test_cart_rejects_duplicate_foreign_unavailable_and_noninteger_quantities(self):
        foreign=Restaurant.objects.create(name='Autre',city='port_gentil',address='A',latitude=0,longitude=0,is_active=True,is_open=True)
        item=RestaurantMenuItem.objects.create(restaurant=foreign,name='Autre plat',price=1000)
        for cart in ([],[{'item_id':item.pk,'quantity':1}],self.payload['cart']*2,[{'item_id':self.dish.pk,'quantity':0}],[{'item_id':self.dish.pk,'quantity':1.2}]):
            response=self.call(DeliveryRequestViewSet,'quote',{**self.payload,'cart':cart})
            self.assertEqual(response.status_code,400,response.data)
        self.dish.is_available=False;self.dish.save()
        self.assertEqual(self.call(DeliveryRequestViewSet,'quote',self.payload).status_code,400)

    def test_menu_price_change_invalidates_quote_and_advance_payment_is_rejected(self):
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':1500}):
            quote=self.call(DeliveryRequestViewSet,'quote',self.payload)
        self.dish.price=3000;self.dish.save()
        response=self.call(DeliveryRequestViewSet,'create',{**self.payload,'quote_token':quote.data['quote_token']})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.call(DeliveryRequestViewSet,'quote',{**self.payload,'payment_timing':'now'}).status_code,400)
        self.assertEqual(RestaurantOrder.objects.count(),0)

    def test_manager_permissions_and_private_contract_not_in_catalog(self):
        for view,action in [(RestaurantAdmin,'list'),(MenuAdmin,'list'),(OperatorOrders,'list')]:
            self.assertEqual(self.call(view,action,user=self.user,method='get').status_code,403)
        response=self.call(RestaurantCatalog,'list',method='get')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('settlement_terms',response.data[0])

    def test_operator_requires_real_payment_reference_or_contract_and_version(self):
        req,_=self.checkout();order=req.restaurant_order
        self.assertEqual(self.operator(order,'ready').status_code,400)
        self.assertEqual(self.operator(order,'confirm',reference='R1',funding='paid').status_code,400)
        self.assertEqual(self.operator(order,'confirm',reference='R1',funding='contract_due').status_code,400)
        response=self.operator(order,'confirm',reference='R1',funding='paid',paid=True,payment_reference='Ticket R1')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(self.operator(order,'ready').status_code,409)
        order.refresh_from_db()
        self.assertEqual(self.operator(order,'ready').status_code,200)
        req.refresh_from_db();self.assertEqual(req.status,'searching')
        self.assertEqual(RestaurantOrderEvent.objects.count(),2)

    def test_periodic_contract_snapshotted_and_settlement_does_not_debit_client(self):
        self.restaurant.periodic_payment=True;self.restaurant.settlement_terms='Chaque vendredi';self.restaurant.save()
        req,_=self.checkout();order=req.restaurant_order
        self.assertEqual(self.operator(order,'confirm',reference='R2',funding='contract_due').status_code,200)
        order.refresh_from_db();self.assertEqual(order.settlement_terms,'Chaque vendredi')
        self.assertIsNone(order.restaurant_paid_at)
        self.assertEqual(self.operator(order,'settle_restaurant',paid=True,payment_reference='Virement réel #1').status_code,200)
        req.refresh_from_db();self.assertFalse(req.is_paid)
        order.refresh_from_db();self.assertEqual(self.operator(order,'settle_restaurant',paid=True,payment_reference='Virement réel #1').status_code,400)

    def make_collected(self):
        req,_=self.checkout();order=req.restaurant_order
        order.state='ready';order.funding='paid';order.save()
        req.status='picked_up';req.assigned_driver=self.driver;req.final_price=1500;req.save()
        return req,order

    def test_cash_collects_total_and_allocates_only_delivery_once(self):
        req,order=self.make_collected()
        with patch('Delivery.views.publish_delivery_status'):
            wrong=self.call(DeliveryDriverViewSet,'confirm_cash',{'received':True,'amount':1500},self.driver_user,req.pk)
            self.assertEqual(wrong.status_code,400)
            for _ in range(2):
                result=self.call(DeliveryDriverViewSet,'confirm_cash',{'received':True,'amount':6500},self.driver_user,req.pk)
                self.assertEqual(result.status_code,200,result.data)
        req.refresh_from_db();order.refresh_from_db();self.driver.refresh_from_db()
        self.assertEqual(order.collected_total,6500)
        self.assertEqual(req.final_price,1500)
        self.assertEqual(req.payment_allocation.platform_amount,330)
        self.assertEqual(self.driver.total_earnings,1170)
        self.assertEqual(order.events.filter(action='collect_cash').count(),1)

    def test_confirmed_restaurant_order_cannot_be_cancelled_silently(self):
        req,_=self.checkout();order=req.restaurant_order;order.state='confirmed';order.save()
        self.assertEqual(self.call(DeliveryRequestViewSet,'cancel',{},pk=req.pk).status_code,409)
        req.refresh_from_db();self.assertNotEqual(req.status,'cancelled')

    def receipt(self,service,pk,user=None,download=False):
        request=APIRequestFactory().get('/?download=pdf' if download else '/')
        force_authenticate(request,user or self.user)
        return ReceiptView.as_view()(request,service=service,pk=pk)

    def test_receipt_private_paid_only_frozen_and_pdf(self):
        req,order=self.make_collected()
        self.assertEqual(self.receipt('delivery',req.pk).status_code,400)
        req.status='delivered';req.is_paid=True;req.cash_received_at=timezone.now();req.save()
        order.collected_at=timezone.now();order.collected_total=6500;order.save()
        first=self.receipt('delivery',req.pk)
        self.assertEqual(first.status_code,200,first.data)
        self.assertEqual(first.data['snapshot']['total'],'6500')
        self.assertEqual(first.data['snapshot']['issuer']['name'],'Blaze')
        self.assertEqual(self.receipt('delivery',req.pk,self.other).status_code,404)
        req.final_price=9999;req.save();self.dish.name='Nouveau nom';self.dish.save()
        self.assertEqual(self.receipt('delivery',req.pk).data,first.data)
        self.assertEqual(CustomerReceipt.objects.count(),1)
        pdf=self.receipt('delivery',req.pk,download=True)
        self.assertEqual(pdf.status_code,200);self.assertTrue(pdf.content.startswith(b'%PDF'))
        self.assertEqual(pdf['Cache-Control'],'no-store, private')

    def test_vtc_receipt_requires_matching_successful_payment(self):
        from RideVTC.models import Ride,Payment
        ride=Ride.objects.create(user=self.user,pickup_location='A',dropoff_location='B',distance_km=2,price=1500,status='completed',completed_at=timezone.now())
        self.assertEqual(self.receipt('vtc',ride.pk).status_code,400)
        payment=Payment.objects.create(ride=ride,amount=1000,wallet='CASH',provider='CASH',status='SUCCESS',idempotency_key='test-ride')
        self.assertEqual(self.receipt('vtc',ride.pk).status_code,400)
        payment.amount=1500;payment.save()
        self.assertEqual(self.receipt('vtc',ride.pk).status_code,200)
        self.assertEqual(self.receipt('vtc',ride.pk,self.other).status_code,404)
    def test_grouped_settlement_is_atomic_idempotent_and_protected(self):
        self.restaurant.periodic_payment=True;self.restaurant.settlement_terms='Chaque vendredi';self.restaurant.save()
        req1,_=self.checkout();order1=req1.restaurant_order
        req2,_=self.checkout({**self.payload,'checkout_id':str(uuid.uuid4())});order2=req2.restaurant_order
        for order in (order1,order2):
            self.assertEqual(self.operator(order,'confirm',reference='Commande réelle',funding='contract_due').status_code,200)
            order.refresh_from_db()
        body={'restaurant':self.restaurant.pk,'orders':[{'id':o.pk,'version':o.version} for o in (order1,order2)],'idempotency_key':str(uuid.uuid4()),'payment_reference':'Virement groupé réel','paid':True}
        self.assertEqual(self.call(OperatorOrders,'settle_batch',body).status_code,403)
        invalid={**body,'orders':[body['orders'][0],{**body['orders'][1],'version':99}]}
        self.assertEqual(self.call(OperatorOrders,'settle_batch',invalid,self.staff).status_code,409)
        order1.refresh_from_db();self.assertIsNone(order1.restaurant_paid_at)
        first=self.call(OperatorOrders,'settle_batch',body,self.staff)
        self.assertEqual(first.status_code,201,first.data)
        self.assertEqual(first.data['amount'],10000)
        again=self.call(OperatorOrders,'settle_batch',body,self.staff)
        self.assertEqual(again.status_code,200);self.assertEqual(again.data['id'],first.data['id'])
        from .models import RestaurantSettlement
        self.assertEqual(RestaurantSettlement.objects.count(),1)
        self.assertEqual(RestaurantOrderEvent.objects.filter(action='settle_batch').count(),2)
        for order in (order1,order2):
            order.refresh_from_db();self.assertIsNotNone(order.restaurant_paid_at)
        balance=self.call(OperatorOrders,'balances',user=self.staff,method='get')
        self.assertEqual(balance.data['restaurants'],[])

    def test_cash_remittance_requires_exact_amount_and_cannot_be_recorded_twice(self):
        req,order=self.make_collected()
        with patch('Delivery.views.publish_delivery_status'):
            result=self.call(DeliveryDriverViewSet,'confirm_cash',{'received':True,'amount':6500},self.driver_user,req.pk)
        self.assertEqual(result.status_code,200)
        order.refresh_from_db()
        balance=self.call(OperatorOrders,'balances',user=self.staff,method='get')
        self.assertEqual(str(balance.data['couriers'][0]['amount']),'5330')
        self.assertEqual(self.operator(order,'record_remittance',received=True,amount=6500,payment_reference='R').status_code,400)
        self.assertEqual(self.operator(order,'record_remittance',received=True,amount=5330,payment_reference='Reversement réel').status_code,200)
        order.refresh_from_db();self.assertEqual(order.cash_remitted_amount,5330)
        self.assertEqual(self.operator(order,'record_remittance',received=True,amount=5330,payment_reference='R').status_code,400)
        self.assertEqual(self.call(OperatorOrders,'balances',user=self.staff,method='get').data['couriers'],[])
        self.assertEqual(order.events.filter(action='record_remittance').count(),1)

    def test_receipt_list_excludes_partial_vtc_payment_and_base64_is_pdf(self):
        import base64
        from RideVTC.models import Ride,Payment
        ride=Ride.objects.create(user=self.user,pickup_location='A',dropoff_location='B',distance_km=2,price=1500,status='completed',completed_at=timezone.now())
        payment=Payment.objects.create(ride=ride,amount=1000,wallet='CASH',provider='CASH',status='SUCCESS',idempotency_key='list-test')
        def listing():
            request=APIRequestFactory().get('/');force_authenticate(request,self.user)
            return ReceiptList.as_view()(request)
        self.assertEqual(listing().data,[])
        payment.amount=1500;payment.save()
        self.assertEqual(listing().data[0]['id'],ride.pk)
        request=APIRequestFactory().get('/?download=base64');force_authenticate(request,self.user)
        response=ReceiptView.as_view()(request,service='vtc',pk=ride.pk)
        self.assertEqual(response.data['content_type'],'application/pdf')
        self.assertTrue(base64.b64decode(response.data['base64']).startswith(b'%PDF-'))
        self.assertEqual(response['Cache-Control'],'no-store, private')

    def test_dispatch_failure_becomes_retryable_and_never_overwrites_assignment(self):
        from .restaurant_api import dispatch_restaurant
        req,_=self.checkout();req.status='searching';req.save()
        with patch('Delivery.services.dispatch.create_offers_for_request',side_effect=RuntimeError('provider unavailable')),patch('Delivery.services.dispatch.publish_delivery_status'):
            with self.assertLogs('Delivery.restaurant_api',level='ERROR'):
                dispatch_restaurant(req.pk)
        req.refresh_from_db();self.assertEqual(req.status,'no_driver')
        req.status='assigned';req.assigned_driver=self.driver;req.save()
        with patch('Delivery.services.dispatch.create_offers_for_request') as dispatch:
            dispatch_restaurant(req.pk)
            dispatch.assert_not_called()

    def test_operator_rejection_requires_reason_and_never_reverses_confirmed_orders(self):
        req,_=self.checkout();order=req.restaurant_order
        self.assertEqual(self.operator(order,'reject').status_code,400)
        with patch('Delivery.services.dispatch.publish_delivery_status'):
            response=self.operator(order,'reject',reason='Restaurant fermé exceptionnellement')
        self.assertEqual(response.status_code,200,response.data)
        req.refresh_from_db();self.assertEqual(req.status,'failed');self.assertFalse(req.is_paid)
        self.assertEqual(order.events.get(action='reject').details['reason'],'Restaurant fermé exceptionnellement')
        req2,_=self.checkout({**self.payload,'checkout_id':str(uuid.uuid4())});other=req2.restaurant_order
        other.state='confirmed';other.save()
        self.assertEqual(self.operator(other,'reject',reason='Fermé').status_code,400)

    def test_restaurant_quote_rejects_a_total_above_cash_collection_limit(self):
        self.dish.price=500000;self.dish.save()
        with patch('Delivery.services.quotes.get_directions',return_value={'distance_m':1500}):
            response=self.call(DeliveryRequestViewSet,'quote',self.payload)
        self.assertEqual(response.status_code,400,response.data)
        self.assertEqual(RestaurantOrder.objects.count(),0)
