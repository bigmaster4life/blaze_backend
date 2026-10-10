# Delivery/consumers.py
from urllib.parse import parse_qs
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser
from blaze_backend.ws_permissions import authentication_error, driver_allowed

from Delivery.utils.rooms import delivery_driver_room, delivery_pool_room

logger = logging.getLogger("delivery")

CURRENT_DELIVERY_DRIVER_SOCKETS: dict[str, str] = {}

class DeliveryDriverConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user_id = None
        # on considère driver_id = user.id (comme RideVTC)
        try:
            self.user_id = int(self.scope["url_route"]["kwargs"].get("driver_id"))
        except (TypeError, ValueError):
            await self.close()
            return

        user = self.scope.get("user", AnonymousUser())
        auth_id = getattr(user, "id", None)
        error = authentication_error(user, self.user_id)
        if error:
            await self.close(code=error)
            return
        if not await driver_allowed(self.user_id, delivery=True):
            await self.close(code=4403)
            return


        q = parse_qs(self.scope.get("query_string", b"").decode())
        city = (q.get("city", [""])[0] or "").strip()
        vehicle = (q.get("vehicle", [""])[0] or "").strip()

        # group perso + group pool (city+vehicle)
        self.group_driver = delivery_driver_room(self.user_id)
        self.group_pool = delivery_pool_room(city or "default", vehicle or "default")

        # anti double socket par livreur
        try:
            old = CURRENT_DELIVERY_DRIVER_SOCKETS.get(str(self.user_id))
            if old and old != self.channel_name:
                try:
                    await self.channel_layer.send(old, {"type": "kick", "reason": "duplicate"})
                except Exception:
                    pass
        finally:
            CURRENT_DELIVERY_DRIVER_SOCKETS[str(self.user_id)] = self.channel_name

        logger.info(
            "[WS][DeliveryDriver] CONNECT path_id=%s auth_id=%s groups=%s,%s",
            self.user_id, auth_id, self.group_driver, self.group_pool
        )

        await self.channel_layer.group_add(self.group_driver, self.channel_name)
        await self.channel_layer.group_add(self.group_pool, self.channel_name)
        await self.accept()

        await self.send_json({
            "type": "hello",
            "role": "delivery_driver",
            "groups": [self.group_driver, self.group_pool],
        })

    async def kick(self, event):
        await self.close(code=4001)

    async def disconnect(self, code):
        try:
            if hasattr(self, "group_driver"):
                await self.channel_layer.group_discard(self.group_driver, self.channel_name)
            if hasattr(self, "group_pool"):
                await self.channel_layer.group_discard(self.group_pool, self.channel_name)
        finally:
            try:
                cur = CURRENT_DELIVERY_DRIVER_SOCKETS.get(str(self.user_id))
                if cur == self.channel_name:
                    del CURRENT_DELIVERY_DRIVER_SOCKETS[str(self.user_id)]
            except Exception:
                pass

        logger.info("[WS][DeliveryDriver] DISCONNECT user_id=%s code=%s", self.user_id, code)

    async def receive_json(self, content, **kwargs):
        t = content.get("type")

        # ping/pong (optionnel)
        if t == "ping":
            await self.send_json({"type": "pong"})
            return

    # message générique (comme RideVTC)
    async def evt(self, event):
        await self.send_json({"event": event["event"], "payload": event.get("payload")})

    # format direct (optionnel)
    async def delivery_offer_new(self, event):
        await self.send_json({
            "event": "delivery.offer.new",
            "payload": event.get("payload") or {}
        })

class DeliveryClientConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user_id = None
        try:
            self.user_id = int(self.scope["url_route"]["kwargs"]["user_id"])
        except Exception:
            await self.close()
            return

        error = authentication_error(self.scope.get("user"), self.user_id)
        if error:
            await self.close(code=error)
            return

        self.group_user = f"delivery.user.{self.user_id}"

        await self.channel_layer.group_add(self.group_user, self.channel_name)
        await self.accept()

        logger.info("[WS][DeliveryClient] CONNECT user_id=%s group=%s",
                    self.user_id, self.group_user)

    async def disconnect(self, code):
        if hasattr(self, "group_user"):
            await self.channel_layer.group_discard(self.group_user, self.channel_name)

        logger.info("[WS][DeliveryClient] DISCONNECT user_id=%s code=%s",
                    self.user_id, code)

    async def evt(self, event):
        await self.send_json({
            "event": event["event"],
            "payload": event.get("payload"),
        })