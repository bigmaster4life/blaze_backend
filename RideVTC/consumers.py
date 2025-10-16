# RideVTC/consumers.py
from urllib.parse import parse_qs
import re
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from .models import Ride
from .utils.rooms import user_room, driver_room, pool_room

logger = logging.getLogger("rides")

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _clean(value: str, default: str) -> str:
    """Sanitise une string pour l’utiliser dans un nom de groupe Channels."""
    v = (value or default or "").strip().lower()
    v = re.sub(r"[^0-9A-Za-z._-]", "_", v)
    return (v[:50] or default)

# (optionnel) check online côté DB
@database_sync_to_async
def _driver_is_online(driver_id: int) -> bool:
    try:
        from .models import DriverPresence
        rec = DriverPresence.objects.filter(driver_id=driver_id).first()
        if rec:
            return bool(getattr(rec, "online", False))
    except Exception:
        pass

    try:
        from .models import Driver
        d = Driver.objects.filter(id=driver_id).first()
        if d and hasattr(d, "online"):
            return bool(d.online)
    except Exception:
        pass

    try:
        from .models import DriverProfile
        p = DriverProfile.objects.filter(driver_id=driver_id).first()
        if p and hasattr(p, "online"):
            return bool(p.online)
    except Exception:
        pass

    return False


# ──────────────────────────────────────────────────────────────
# AppConsumer (clients: /ws/app/?role=customer&user_id=...)
# ──────────────────────────────────────────────────────────────

class AppConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.groups_to_join = []

        qs = parse_qs(self.scope.get("query_string", b"").decode())
        raw_role = (qs.get("role", [""])[0] or "").lower()

        role = {
            "client": "customer",
            "customer": "customer",
            "chauffeur": "driver",
            "driver": "driver",
        }.get(raw_role, raw_role)

        user = self.scope.get("user", AnonymousUser())

        if role in ("client", "customer"):
            user_id = getattr(user, "id", None) or int(qs.get("user_id", ["0"])[0])
            if user_id:
                g = f"user.{user_id}"  # ⚠️ point, pas deux-points
                self.groups_to_join.append(g)
                logger.info("[WS][App] customer join group=%s", g)

        # On n’abonne pas les chauffeurs ici (ils passent par DriverConsumer)
        for g in self.groups_to_join:
            await self.channel_layer.group_add(g, self.channel_name)

        await self.accept()
        logger.info("[WS][App] CONNECTED role=%s groups=%s", role, self.groups_to_join)

    async def disconnect(self, code):
        for g in getattr(self, "groups_to_join", []):
            await self.channel_layer.group_discard(g, self.channel_name)
        logger.info("[WS][App] DISCONNECT (%s)", code)

    async def receive_json(self, content, **kwargs):
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})

    # Format générique {event, payload}
    async def evt(self, event):
        await self.send_json({"event": event["event"], "payload": event.get("payload")})

    # Format direct "ride.accepted"
    async def ride_accepted(self, event):
        await self.send_json({
            "event": "ride.accepted",
            "payload": {
                "requestId": event.get("requestId"),
                "driver": event.get("driver"),
                "ride": event.get("ride"),
            }
        })

    async def ride_arrived(self, event):
        await self.send_json({
            "event": "ride.arrived",
            "payload": {
                "requestId": event.get("requestId"),
                "driverId": event.get("driverId"),
                "lat": event.get("lat"),
                "lng": event.get("lng"),
                "source": event.get("source"),
            }
        })


# ──────────────────────────────────────────────────────────────
# DriverConsumer (/ws/rides/driver/<driver_id>/?area=...&category=...)
# ──────────────────────────────────────────────────────────────

# registre anti-doublon: 1 socket active par driver (driver_id -> channel_name)
CURRENT_DRIVER_SOCKETS: dict[str, str] = {}

class DriverConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # Param path
        self.driver_id = self.scope["url_route"]["kwargs"].get("driver_id")

        # Query params
        q = parse_qs(self.scope.get("query_string", b"").decode())
        raw_area = (q.get("area", ["city-default"])[0] or "city-default")
        raw_category = (q.get("category", ["eco"])[0] or "eco")

        # Sanitize
        self.area = _clean(raw_area, "city-default")
        self.category = _clean(raw_category, "eco")

        # Groupes: pool + perso driver
        self.group_pool   = pool_room(self.category, self.area)          # ex: "pool.eco.city-default"
        self.group_driver = driver_room(int(self.driver_id))             # ex: "driver.1"

        # Anti-doublon: si une autre socket existe pour ce driver, on la "kick"
        try:
            old = CURRENT_DRIVER_SOCKETS.get(str(self.driver_id))
            if old and old != self.channel_name:
                try:
                    await self.channel_layer.send(old, {"type": "kick", "reason": "duplicate"})
                    logger.info("[WS] kick previous socket for driver#%s (duplicate)", self.driver_id)
                except Exception as e:
                    logger.exception("kick previous socket failed: %s", e)
        finally:
            CURRENT_DRIVER_SOCKETS[str(self.driver_id)] = self.channel_name

        logger.info(
            "[WS] driver#%s WSCONNECT area=%s cat=%s → groups=%s / %s",
            self.driver_id, self.area, self.category, self.group_pool, self.group_driver
        )

        try:
            await self.channel_layer.group_add(self.group_pool, self.channel_name)
            await self.channel_layer.group_add(self.group_driver, self.channel_name)
            await self.accept()
            logger.info("[WS] driver#%s JOINED groups %s & %s", self.driver_id, self.group_pool, self.group_driver)
        except Exception as e:
            logger.exception("DriverConsumer.connect error: %s", e)
            await self.close()

    async def kick(self, event):
        """Fermeture à la demande (ex: connexion en double)."""
        await self.close(code=4001)

    async def disconnect(self, code):
        try:
            if hasattr(self, "group_pool"):
                await self.channel_layer.group_discard(self.group_pool, self.channel_name)
            if hasattr(self, "group_driver"):
                await self.channel_layer.group_discard(self.group_driver, self.channel_name)
            logger.info("[WS] driver#%s LEFT groups (code=%s)", self.driver_id, code)
        finally:
            try:
                cur = CURRENT_DRIVER_SOCKETS.get(str(self.driver_id))
                if cur == self.channel_name:
                    del CURRENT_DRIVER_SOCKETS[str(self.driver_id)]
            except Exception:
                pass
            logger.info("[WS] driver#%s DISCONNECT (%s)", self.driver_id, code)

    # passe-plat générique (compat `{"type":"evt", "event": "...", "payload": {...}}`)
    async def evt(self, event):
        await self.send_json({"event": event["event"], "payload": event.get("payload")})

    # compat format direct "ride.cancelled"
    async def ride_cancelled(self, event):
        await self.send_json({"event": "ride.cancelled", "payload": {"requestId": event.get("requestId")}})

    async def receive_json(self, content, **kwargs):
        logger.info("[WS][Driver] recv from driver#%s → %s", self.driver_id, content)
        t = content.get("type")
        if t == "ping":
            await self.send_json({"type": "pong"})
            return
        
        if t == "driver.arrived":
            # 1) Normalisation des champs d'entrée
            ride_raw = content.get("rideId") or content.get("requestId")
            try:
                ride_id = int(ride_raw)
            except (TypeError, ValueError):
                await self.send_json({"type": "error", "message": "rideId invalid"})
                return
            # lat/lng optionnels → si invalide, on les ignore (None)
            lat_raw = content.get("lat", None)
            lng_raw = content.get("lng", None)
            try:
                lat = float(lat_raw) if lat_raw is not None else None
                lng = float(lng_raw) if lng_raw is not None else None
            except (TypeError, ValueError):
                lat = None
                lng = None
            source = (content.get("source") or "manual").strip().lower()[:20]

            # 2) Marquer arrivé (DB) + récupérer user_id
            ok, payload = await self._mark_arrived_and_get_payload(ride_id, lat, lng, source)
            if not ok:
                await self.send_json({"type": "error", "message": payload or "cannot mark arrived"})
                return
            
            user_id = payload["user_id"]
            if not user_id:
                await self.send_json({"type": "error", "message": "ride has no user_id"})
                return
            
            # 3) Broadcast aux clients (format générique + format direct)
            ch = getattr(self, "channel_layer", None)
            if not ch:
                # Pas de layer → on log et on s'arrête proprement (évite crash)
                logger.warning("[WS] channel_layer missing → skip ride.arrived broadcast (ride_id=%s)", ride_id)
                await self.send_json({"type": "ok", "event": "ride.arrived.ack", "rideId": ride_id})
                return
            
            client_groups = {user_room(user_id), f"user.{user_id}"}
            now_iso = timezone.now().isoformat()

            # payload générique
            evt_payload = {
                "requestId": ride_id,
                "driver": { "id": int(self.driver_id) },
                "loc": {"lat": lat, "lng": lng} if (lat is not None and lng is not None) else None,
                "source": source,
                "at": now_iso,
                "grace": 300,
            }

            # 1) format générique
            for g in client_groups:
                await ch.group_send(
                    g,
                    {
                        "type": "evt",
                        "event": "ride.arrived",
                        "payload": {
                            **evt_payload,
                            "loc": evt_payload["loc"] or {},  # for compatibility
                        },
                    },
                )
            # 2) format direct
                direct_msg = {
                    "type": "ride.arrived",
                    "requestId": ride_id,
                    "driverId": int(self.driver_id),
                    "source": source,
                    "grace": 300,
                }
                if lat is not None and lng is not None:
                    direct_msg.update({"lat": lat, "lng": lng})
                await ch.group_send(g, direct_msg)
            
            logger.info(
                "[WS] broadcast ride.arrived → groups=%s requestId=%s driver#%s",
                list(client_groups), ride_id, self.driver_id
            )
            # petit ack au chauffeur (utile côté UI chauffeur)
            await self.send_json({"type": "ok", "event": "ride.arrived.ack", "rideId": ride_id})
            return
            

    async def ride_requested(self, event):
        """Reçoit depuis la vue (create) et pousse au chauffeur."""
        try:
            ride = event.get("ride", {})
            logger.info("[WS] → driver#%s recv ride.requested id=%s group=%s",
                        self.driver_id, ride.get("id"), self.group_pool)
            await self.send_json({"type": "ride.requested", "ride": ride})
        except Exception as e:
            logger.exception("ride_requested send error: %s", e)

    @database_sync_to_async
    def _accept_ride(self, ride_id: int):
        if not ride_id:
            return False, "ride_id missing"

        try:
            r = Ride.objects.select_for_update().get(id=ride_id)
        except Ride.DoesNotExist:
            return False, "Ride not found"

        if r.status != "pending":
            return False, f"Ride already {r.status}"

        try:
            r.driver_id = int(self.driver_id)
        except Exception:
            r.driver_id = None

        r.status = "accepted"
        r.accepted_at = timezone.now()
        r.save(update_fields=["driver_id", "status", "accepted_at"])

        return True, {
            "user_id": r.user_id,
            "category": getattr(r, "category", "eco"),
            "pickup_location": getattr(r, "pickup_location", None),
            "dropoff_location": getattr(r, "dropoff_location", None),
            "pickup_lat": getattr(r, "pickup_lat", None),
            "pickup_lng": getattr(r, "pickup_lng", None),
            "dropoff_lat": getattr(r, "dropoff_lat", None),
            "dropoff_lng": getattr(r, "dropoff_lng", None),
            "price": str(getattr(r, "price", "")),
        }
    
    @database_sync_to_async
    def _mark_arrived_and_get_payload(self, ride_id: int, lat: float, lng: float, source: str):
        if not ride_id:
            return False, "ride_id missing"
        try:
            r = Ride.objects.select_for_update().get(id=ride_id)
        except Ride.DoesNotExist:
            return False, "Ride not found"

        if hasattr(r, "arrived_at") and not r.arrived_at:
            r.arrived_at = timezone.now()
            r.save(update_fields=["arrived_at"])

        return True, {"user_id": r.user_id}