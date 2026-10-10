from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, override_settings
from RideVTC.routing import websocket_urlpatterns as rides
from analytics.routing import websocket_urlpatterns as ops
from Delivery.routing import websocket_urlpatterns as deliveries
from RideVTC.consumers import AppConsumer, DriverConsumer, CURRENT_DRIVER_SOCKETS
from blaze_backend.channels_jwt import JwtAuthMiddlewareStack


def user(pk=41, active=True):
    return SimpleNamespace(pk=pk, id=pk, is_authenticated=True, is_active=active, is_staff=False, is_superuser=False)


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
class WebSocketSecurityTests(SimpleTestCase):
    def check_connection(self, path, account, accepted, code=None, allowed=True):
        async def check():
            app = URLRouter(rides + deliveries + ops)
            socket = WebsocketCommunicator(app, path)
            socket.scope["user"] = account
            with patch("RideVTC.consumers.driver_allowed", AsyncMock(return_value=allowed)), patch("Delivery.consumers.driver_allowed", AsyncMock(return_value=allowed)):
                connected, detail = await socket.connect()
                self.assertEqual(connected, accepted)
                if code is not None:
                    self.assertEqual(detail, code)
                await socket.disconnect()
        async_to_sync(check)()

    def test_customer_anonymous_denied(self):
        self.check_connection("/ws/app/?role=customer&user_id=41", AnonymousUser(), False, 4401)

    def test_customer_other_id_denied(self):
        self.check_connection("/ws/app/?role=customer&user_id=42", user(), False, 4403)

    def test_customer_bad_id_denied(self):
        self.check_connection("/ws/app/?role=customer&user_id=abc", user(), False, 4403)

    def test_customer_own_id_allowed(self):
        self.check_connection("/ws/app/?role=customer&user_id=41", user(), True)

    def test_customer_without_id_uses_authenticated_account(self):
        self.check_connection("/ws/app/?role=customer", user(), True)

    def test_inactive_denied(self):
        self.check_connection("/ws/app/?role=customer", user(active=False), False, 4401)

    def test_driver_anonymous_does_not_kick_existing_socket(self):
        CURRENT_DRIVER_SOCKETS["41"] = "existing"
        try:
            self.check_connection("/ws/rides/driver/41/", AnonymousUser(), False, 4401)
            self.assertEqual(CURRENT_DRIVER_SOCKETS["41"], "existing")
        finally:
            CURRENT_DRIVER_SOCKETS.clear()

    def test_driver_other_id_denied(self):
        self.check_connection("/ws/rides/driver/42/", user(), False, 4403)

    def test_driver_without_allowed_profile_denied(self):
        self.check_connection("/ws/rides/driver/41/", user(), False, 4403, allowed=False)

    def test_driver_own_id_allowed(self):
        self.check_connection("/ws/rides/driver/41/", user(), True)

    def test_delivery_driver_other_id_denied(self):
        self.check_connection("/ws/delivery/driver/42/", user(), False, 4403)

    def test_delivery_driver_own_id_allowed(self):
        self.check_connection("/ws/delivery/driver/41/", user(), True)

    def test_delivery_customer_anonymous_denied(self):
        self.check_connection("/ws/delivery/client/41/", AnonymousUser(), False, 4401)

    def test_delivery_customer_other_id_denied(self):
        self.check_connection("/ws/delivery/client/42/", user(), False, 4403)

    def test_delivery_customer_own_id_allowed(self):
        self.check_connection("/ws/delivery/client/41/", user(), True)

    def test_ops_anonymous_denied(self):
        self.check_connection("/ws/ops/", AnonymousUser(), False, 4401)

    def test_ops_nonstaff_denied(self):
        self.check_connection("/ws/ops/", user(), False, 4403)

    def test_ops_staff_allowed(self):
        staff = user()
        staff.is_staff = True
        self.check_connection("/ws/ops/", staff, True)

    def test_header_token_passed_to_validator(self):
        async def check():
            app = JwtAuthMiddlewareStack(URLRouter(rides))
            socket = WebsocketCommunicator(app, "/ws/app/?role=customer", headers=[(b"authorization", b"Bearer sample-token")])
            with patch("blaze_backend.channels_jwt.get_user_for_token", AsyncMock(return_value=user())) as validate:
                self.assertTrue((await socket.connect())[0])
                validate.assert_awaited_once_with("sample-token")
                await socket.disconnect()
        async_to_sync(check)()

    def test_invalid_token_denied(self):
        async def check():
            app = JwtAuthMiddlewareStack(URLRouter(rides))
            socket = WebsocketCommunicator(app, "/ws/app/?role=customer&token=invalid")
            self.assertEqual(await socket.connect(), (False, 4401))
            await socket.disconnect()
        async_to_sync(check)()

    def test_customer_cannot_chat_in_another_ride(self):
        async def check():
            consumer = AppConsumer()
            consumer.user_id = 41
            consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
            consumer.send_json = AsyncMock()
            with patch("RideVTC.consumers._get_ride_partners", AsyncMock(return_value={"user_id": 42, "driver_id": 43})):
                await consumer._handle_chat_from_customer({"rideId": 1, "text": "hello"})
            consumer.channel_layer.group_send.assert_not_awaited()
        async_to_sync(check)()

    def test_driver_cannot_chat_in_unassigned_ride(self):
        async def check():
            consumer = DriverConsumer()
            consumer.user_id = 41
            consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
            consumer.send_json = AsyncMock()
            with patch("RideVTC.consumers._get_ride_partners", AsyncMock(return_value={"user_id": 42, "driver_id": None})):
                await consumer._handle_chat_from_driver({"rideId": 1, "text": "hello"})
            consumer.channel_layer.group_send.assert_not_awaited()
        async_to_sync(check)()

from django.test import TransactionTestCase
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from blaze_backend.channels_jwt import get_user_for_token
from blaze_backend.ws_permissions import driver_allowed
from drivers.models import Driver


class WebSocketJWTTests(TransactionTestCase):
    def setUp(self):
        self.account = get_user_model().objects.create_user(
            email="ws-test@example.com", password="test-password", user_type="chauffeur", first_name="WS", last_name="Test"
        )

    def test_valid_access_token_resolves_real_account(self):
        token = str(AccessToken.for_user(self.account))
        resolved = async_to_sync(get_user_for_token)(token)
        self.assertEqual(resolved.pk, self.account.pk)

    def test_inactive_account_token_rejected(self):
        token = str(AccessToken.for_user(self.account))
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        with self.assertRaises(Exception):
            async_to_sync(get_user_for_token)(token)

    def test_expired_token_rejected(self):
        token = AccessToken.for_user(self.account)
        token["exp"] = 1
        with self.assertRaises(Exception):
            async_to_sync(get_user_for_token)(str(token))

    def test_driver_profile_required_and_blocked_profile_rejected(self):
        self.assertFalse(async_to_sync(driver_allowed)(self.account.pk))
        profile = Driver.objects.create(user=self.account, full_name="Test", phone="000", vehicle_plate="TEST")
        self.assertTrue(async_to_sync(driver_allowed)(self.account.pk))
        profile.is_blocked = True
        profile.save(update_fields=["is_blocked"])
        self.assertFalse(async_to_sync(driver_allowed)(self.account.pk))

    def test_arrival_changes_only_assigned_ride(self):
        from RideVTC.models import Ride
        ride = Ride.objects.create(user=self.account, driver=self.account, pickup_location="A", dropoff_location="B", distance_km=1, price=100)
        consumer = DriverConsumer()
        consumer.user_id = self.account.pk + 100
        ok, _ = async_to_sync(consumer._mark_arrived_and_get_payload)(ride.pk, 1, 2, "manual")
        self.assertFalse(ok)
        ride.refresh_from_db()
        self.assertIsNone(ride.driver_lat)
        consumer.user_id = self.account.pk
        ok, _ = async_to_sync(consumer._mark_arrived_and_get_payload)(ride.pk, 1, 2, "manual")
        self.assertTrue(ok)
        ride.refresh_from_db()
        self.assertEqual(ride.driver_lat, 1)
