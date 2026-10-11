from decimal import Decimal
from unittest.mock import patch, Mock
from django.test import TestCase, override_settings
from django.core import signing
from rest_framework.test import APIRequestFactory, force_authenticate
from users.models import CustomUser
from .pricing import fare, split_amount, make_quote, read_quote, QuoteInput, RouteUnavailable
from .quotes import RideQuoteView
from .serializers import RideCreateSerializer
from .models import RidePricing

DATA = dict(pickup_lat=-.7, pickup_lng=8.8, dropoff_lat=-.71, dropoff_lng=8.81, category='clim')

class PricingTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create(email='quote@example.com', user_type='client')

    def quote(self):
        with patch('RideVTC.pricing.route_metrics', return_value=(17.71, 2400)):
            return make_quote(self.user.pk, DATA)

    def serializer(self, **overrides):
        request = APIRequestFactory().post('/')
        request.user = self.user
        payload = {**DATA, 'pickup_location': 'A', 'dropoff_location': 'B', 'distance_km': 1,
                   'price': '1', **overrides}
        return RideCreateSerializer(data=payload, context={'request': request})

    def test_current_tariff_boundaries_and_caps(self):
        for cat, values in {'eco':(1000,1500,4550), 'clim':(1500,2000,5950), 'vip':(2500,3000,9900)}.items():
            self.assertEqual(fare(cat, 2), values[0])
            self.assertEqual(fare(cat, 5), values[1])
            self.assertEqual(fare(cat, 17.71), values[2])
            self.assertEqual(fare(cat, 100), values[2])
        self.assertEqual(fare('eco', '5.0001'), 2500)
        self.assertEqual(fare('eco', '5.0125'), 2510)

    def test_invalid_fares_and_allocations(self):
        for distance in (0, -1, 'NaN', 'Infinity'):
            with self.assertRaises(ValueError): fare('eco', distance)
        with self.assertRaises(ValueError): fare('unknown', 3)
        for amount, rate, discount in [(-1,.22,0),(100,1.1,0),(100,.22,.21),(100,'NaN',0)]:
            with self.assertRaises(ValueError): split_amount(amount, rate, discount)

    def test_22_percent_and_shared_discount_conserve_cash(self):
        plain = split_amount(4550, '.22')
        shared = split_amount(4550, '.22', '.20')
        self.assertEqual(plain, dict(paid_amount=Decimal(4550), platform_amount=Decimal(1001), driver_amount=Decimal(3549)))
        self.assertEqual(shared, dict(paid_amount=Decimal(3640), platform_amount=Decimal(801), driver_amount=Decimal(2839)))
        for price in (1001, 4551, 5953):
            result = split_amount(price, '.22', '.137')
            self.assertEqual(result['paid_amount'], result['driver_amount'] + result['platform_amount'])

    def test_quote_overrides_client_price_and_records_category(self):
        serializer = self.serializer(quote_token=self.quote()['quote_token'])
        self.assertTrue(serializer.is_valid(), serializer.errors)
        ride = serializer.save()
        self.assertEqual(ride.price, '5950')
        self.assertEqual(ride.distance_km, 17.71)
        self.assertEqual(ride.category, 'clim')
        pricing = RidePricing.objects.get(ride=ride)
        self.assertEqual(pricing.commission_rate, Decimal('.22'))
        self.assertEqual(pricing.quoted_duration_seconds, 2400)

    def test_quote_owner_itinerary_and_signature_protected(self):
        token = self.quote()['quote_token']
        for field, value in [('pickup_lat',0),('category','vip')]:
            serializer = self.serializer(quote_token=token, **{field:value})
            self.assertFalse(serializer.is_valid())
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError): read_quote(token, self.user.pk+1, DATA)
        with self.assertRaises(ValidationError): read_quote(token+'x', self.user.pk, DATA)

    def test_expired_quote_rejected(self):
        with patch('django.core.signing.time.time', return_value=1000): token = self.quote()['quote_token']
        with patch('django.core.signing.time.time', return_value=1301):
            serializer = self.serializer(quote_token=token)
            self.assertFalse(serializer.is_valid())

    def test_legacy_supported_until_explicit_activation(self):
        serializer = self.serializer(price='1500')
        self.assertTrue(serializer.is_valid(), serializer.errors)
        ride = serializer.save()
        self.assertEqual(ride.category, 'clim')
        self.assertFalse(RidePricing.objects.filter(ride=ride).exists())
        with override_settings(VTC_REQUIRE_QUOTE=True):
            self.assertFalse(self.serializer().is_valid())

    def test_quote_endpoint_auth_and_validation(self):
        factory = APIRequestFactory()
        self.assertIn(RideQuoteView.as_view()(factory.post('/', DATA)).status_code, (401,403))
        request = factory.post('/', {**DATA, 'pickup_lat': 91})
        force_authenticate(request, self.user)
        self.assertEqual(RideQuoteView.as_view()(request).status_code, 400)
        with patch('RideVTC.pricing.route_metrics', return_value=(17.71,2400)):
            request = factory.post('/', DATA)
            force_authenticate(request, self.user)
            response = RideQuoteView.as_view()(request)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['price'], '5950')

    @override_settings(GOOGLE_KEY='test-only')
    def test_google_numeric_distance_used_and_failures_closed(self):
        from .pricing import route_metrics
        response = Mock()
        response.json.return_value = {'status':'OK','routes':[{'legs':[{'distance':{'value':17710},'duration':{'value':2400}}]}]}
        with patch('RideVTC.pricing.requests.get', return_value=response):
            self.assertEqual(route_metrics(DATA), (17.71,2400))
            response.json.return_value = {'status':'ZERO_RESULTS'}
            with self.assertRaises(RouteUnavailable): route_metrics(DATA)
        import requests
        with patch('RideVTC.pricing.requests.get', side_effect=requests.Timeout):
            with self.assertRaises(RouteUnavailable): route_metrics(DATA)

    def test_menu_computes_route_once_for_all_categories(self):
        from .quotes import RideQuoteMenuView
        request = APIRequestFactory().post('/', DATA)
        force_authenticate(request, self.user)
        with patch('RideVTC.quotes.route_metrics', return_value=(17.71, 2400)) as metrics:
            response = RideQuoteMenuView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([q['price'] for q in response.data['quotes']], ['4550','5950','9900'])
        metrics.assert_called_once()

    def test_accounting_records_once_only_on_success_and_preserves_legacy(self):
        from .models import Payment, PaymentAllocation
        from .accounting import record_allocation, allocated_amount
        from django.db.models import Sum
        serializer = self.serializer(quote_token=self.quote()['quote_token'])
        serializer.is_valid(raise_exception=True)
        ride = serializer.save(driver=self.user)
        payment = Payment.objects.create(ride=ride, amount=5950, wallet='CASH', provider='CASH', status='PENDING', idempotency_key='quoted')
        self.assertFalse(PaymentAllocation.objects.exists())
        payment.status = 'SUCCESS'
        payment.save()
        allocation = payment.allocation
        self.assertEqual(allocation.platform_amount, 1309)
        self.assertEqual(allocation.driver_amount, 4641)
        self.assertEqual(allocation.cash_commission_due, 1309)
        record_allocation(payment)
        payment.save()
        self.assertEqual(PaymentAllocation.objects.count(), 1)
        # Changing the current configured rate does not change frozen payments.
        with override_settings(VTC_QUOTE_COMMISSION_RATE='0.30'):
            record_allocation(payment)
        allocation.refresh_from_db()
        self.assertEqual(allocation.commission_rate, Decimal('.22'))
        serializer = self.serializer(price=1000)
        serializer.is_valid(raise_exception=True)
        legacy = serializer.save()
        Payment.objects.create(ride=legacy, amount=1000, wallet='CASH', provider='CASH', status='SUCCESS', idempotency_key='legacy')
        self.assertEqual(PaymentAllocation.objects.count(), 1)
        totals = Payment.objects.aggregate(driver=Sum(allocated_amount('driver_amount', 1)), platform=Sum(allocated_amount('platform_amount', '.20')))
        self.assertEqual(totals['driver'], 5641)
        self.assertEqual(totals['platform'], 1509)

    def test_online_allocation_has_no_cash_commission_due(self):
        from .models import Payment
        serializer = self.serializer(quote_token=self.quote()['quote_token'])
        serializer.is_valid(raise_exception=True)
        ride = serializer.save()
        payment = Payment.objects.create(ride=ride, amount=5950, wallet='MOBILE_MONEY', status='SUCCESS', idempotency_key='online')
        self.assertEqual(payment.allocation.cash_commission_due, 0)
        self.assertEqual(payment.allocation.driver_amount, 4641)

    def test_earnings_and_history_use_frozen_net_revenue(self):
        from .models import Payment
        from drivers.views import DriverEarningsSummary
        from .history import RideHistoryView
        serializer = self.serializer(quote_token=self.quote()['quote_token'])
        serializer.is_valid(raise_exception=True)
        ride = serializer.save(driver=self.user, status='completed')
        Payment.objects.create(ride=ride, amount=5950, wallet='CASH', provider='CASH', status='SUCCESS', idempotency_key='summary')
        factory = APIRequestFactory()
        request = factory.get('/')
        force_authenticate(request, self.user)
        response = DriverEarningsSummary.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['today'], 4641)
        self.assertEqual(response.data['cash_collected_today'], 5950)
        self.assertEqual(response.data['cash_commission_due_today'], 1309)
        request = factory.get('/', {'role':'driver'})
        force_authenticate(request, self.user)
        response = RideHistoryView.as_view()(request)
        self.assertEqual(response.data['results'][0]['driver_revenue'], '4641.00')

    def test_analytics_uses_new_allocations_and_legacy_rates(self):
        from .models import Payment
        from analytics.views import AnalyticsViewSet
        serializer = self.serializer(quote_token=self.quote()['quote_token'])
        serializer.is_valid(raise_exception=True)
        ride = serializer.save(driver=self.user, status='completed')
        Payment.objects.create(ride=ride, amount=5950, wallet='CASH', provider='CASH', status='SUCCESS', idempotency_key='analytics')
        request = APIRequestFactory().get('/')
        force_authenticate(request, self.user)
        response = AnalyticsViewSet.as_view({'get':'revenue_daily'})(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['commission'], 1309)

    def test_mobile_payment_cannot_replace_server_quote_amount(self):
        # Firebase delivery is outside this payment test and unavailable locally.
        import sys
        with patch.dict(sys.modules, {'notifications.fcm': Mock()}):
            from .views import MobileInitiate
        from .models import Payment
        serializer = self.serializer(quote_token=self.quote()['quote_token'])
        serializer.is_valid(raise_exception=True)
        ride = serializer.save(status='completed')
        request = APIRequestFactory().post('/', {'request_id':ride.pk, 'amount':'1', 'currency':'USD', 'msisdn':'077000000'})
        force_authenticate(request, self.user)
        with patch('RideVTC.views.provider_init_payment', return_value=(True,'test-tx','ok')):
            response = MobileInitiate.as_view()(request)
        self.assertEqual(response.status_code, 201)
        payment = Payment.objects.get(ride=ride)
        self.assertEqual(payment.amount, 5950)
        self.assertEqual(payment.currency, 'XAF')
        payment.status = 'SUCCESS'
        payment.save()
        self.assertEqual(payment.allocation.driver_amount, 4641)
        request = APIRequestFactory().post('/', {'request_id':ride.pk, 'amount':'1', 'msisdn':'077000000'})
        force_authenticate(request, self.user)
        with patch('RideVTC.views.provider_init_payment') as provider:
            self.assertEqual(MobileInitiate.as_view()(request).status_code, 409)
            provider.assert_not_called()
