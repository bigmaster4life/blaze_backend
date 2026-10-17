import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import TestCase
from users.models import CustomUser
from notifications.models import Device


class DeliveryNotificationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Local test environment omits Google SDK; payload/network code is real,
        # OAuth and all external sends are replaced with explicit test doubles.
        modules = {name: Mock() for name in ['google', 'google.oauth2', 'google.auth', 'google.auth.transport', 'google.auth.transport.requests']}
        spec = importlib.util.spec_from_file_location('isolated_fcm_notifications', Path(__file__).resolve().parents[1]/'notifications'/'fcm.py')
        cls.fcm = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.fcm)

    def test_offer_has_sound_on_both_platforms_and_opens_the_exact_offer(self):
        req = SimpleNamespace(id=17, pickup_label='Retrait', dropoff_label='Destination', order_details={})
        payload = self.fcm.payload_delivery_new_order(req, offer_id=29)
        self.assertEqual(payload['android']['notification']['channel_id'], 'be_delivery_offers_v2')
        self.assertEqual(payload['android']['notification']['sound'], 'default')
        self.assertEqual(payload['apns']['payload']['aps']['sound'], 'default')
        self.assertEqual(payload['data']['delivery_request_id'], '17')
        self.assertEqual(payload['data']['delivery_offer_id'], '29')

    def test_delivery_status_is_a_visible_push_on_ios(self):
        payload = self.fcm.payload_client_status_update('delivery', 17, 'assigned')
        self.assertIn('accepté', payload['notification']['body'])
        self.assertEqual(payload['apns']['headers']['apns-priority'], '10')
        self.assertEqual(payload['data']['object_id'], '17')
        self.assertNotIn('notification', self.fcm.payload_client_status_update('ride', 17, 'assigned'))

    def test_client_push_uses_registered_devices_even_without_legacy_token(self):
        client = CustomUser.objects.create(email='push-client@test.invalid')
        Device.objects.create(user=client, token='registered-device', is_active=True)
        Device.objects.create(user=client, token='disabled-device', is_active=False)
        with patch.object(self.fcm, 'send_fcm_message', return_value=True) as send:
            self.assertTrue(self.fcm.notify_client_update(client, 'delivery', 17, 'assigned'))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], 'registered-device')

    def test_reuses_google_credentials_and_limits_oauth_transport_timeout(self):
        credentials = Mock(valid=False, token='test-oauth-token')
        transport = Mock()
        credentials.refresh.side_effect = lambda request: request(url='https://oauth.test.invalid')
        with patch.object(self.fcm, '_credentials', None), patch.object(self.fcm.service_account.Credentials, 'from_service_account_file', return_value=credentials) as load, patch.object(self.fcm, 'Request', return_value=transport):
            self.assertEqual(self.fcm.get_access_token(), 'test-oauth-token')
            credentials.valid = True
            self.assertEqual(self.fcm.get_access_token(), 'test-oauth-token')
            self.assertEqual(load.call_count, 1)
            self.assertEqual(credentials.refresh.call_count, 1)
            self.assertEqual(transport.call_args.kwargs['timeout'], 5)
