# Delivery/services/dispatch.py
import logging
import os

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from django.db import transaction
from django.utils import timezone

from Delivery.models import DeliveryDriver, DeliveryOffer, DeliveryRequest
from Delivery.utils.geo import haversine_km
from Delivery.utils.rooms import delivery_driver_room, delivery_pool_room, delivery_user_room

from notifications.fcm import notify_delivery_driver_new_order, notify_client_update
from RideVTC.utils.rooms import user_room
from notifications.models import Device

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
RADII = [15, 20, 30]               # km
OFFER_TTL_SECONDS = 900            # 15 minutes
MAX_OFFERS_NO_GPS = 10
MAX_ACTIVE_DELIVERIES = 3

ACTIVE_REQUEST_STATUSES = (
    DeliveryRequest.STATUS_ASSIGNED,
    DeliveryRequest.STATUS_PICKED_UP,
)

SMART_DISPATCH = os.getenv("DELIVERY_SMART_DISPATCH", "0") == "1"
CLUSTER_RADIUS_KM = float(os.getenv("DELIVERY_CLUSTER_RADIUS_KM", "1.5"))

# WS events (doivent matcher ton frontend)
WS_EVENT_NEW_OFFER = "delivery.offer.new"
WS_EVENT_REQUEST_STATUS = "delivery.request.update"


# ─────────────────────────────────────────────
# WS helpers (pool + ciblé + client)
# ─────────────────────────────────────────────
def _ws_send_group(group: str, event: str, payload: dict) -> None:
    """
    Envoie au consumer DeliveryDriverConsumer.evt():
      {"type":"evt","event":"...", "payload": {...}}
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            group,
            {"type": "evt", "event": event, "payload": payload},
        )
    except Exception as e:
        logger.warning("[DELIVERY][WS] group_send failed group=%s event=%s err=%s", group, event, e)


def _offer_payload_for_app(offer: DeliveryOffer) -> dict:
    """
    Payload EXACT attendu par DeliveryOffersScreen.tsx:
    { id, request_id, pickup_label, dropoff_label, city, estimated_price, distance_km, status, expires_at }
    """
    req = offer.request
    return {
        "id": offer.id,
        "request_id": req.id,
        "pickup_label": getattr(req, "pickup_label", None) or getattr(req, "pickup_address", None) or "",
        "dropoff_label": getattr(req, "dropoff_label", None) or getattr(req, "dropoff_address", None) or "",
        "city": getattr(req, "city", "") or "",
        "estimated_price": getattr(req, "estimated_price", None) or getattr(req, "price", None) or 0,
        "distance_km": float(getattr(offer, "distance_km", 0) or 0),
        "status": getattr(offer, "status", "") or "",
        "expires_at": offer.expires_at.isoformat() if getattr(offer, "expires_at", None) else None,
    }


def _broadcast_offer_created(offer: DeliveryOffer) -> None:
    """
    1) ciblé: delivery.driver.<driver_user_id>
    2) pool:  delivery.pool.<city>.<vehicle_type>
    """
    try:
        req = offer.request
        driver = offer.driver

        payload = _offer_payload_for_app(offer)

        # ciblé livreur (ton consumer attend driver_id = user.id)
        driver_user_id = getattr(driver, "user_id", None)
        if driver_user_id:
            _ws_send_group(delivery_driver_room(int(driver_user_id)), WS_EVENT_NEW_OFFER, payload)

        # pool ville + véhicule
        _ws_send_group(delivery_pool_room(req.city, req.vehicle_type), WS_EVENT_NEW_OFFER, payload)

    except Exception as e:
        logger.warning("[DELIVERY][WS] _broadcast_offer_created error offer=%s err=%s", getattr(offer, "id", None), e)


def _broadcast_request_status_to_client(req: DeliveryRequest) -> None:
    """
    Envoie au client: delivery.user.<user_id>
    """
    try:
        user_id = getattr(req, "user_id", None)
        if not user_id and getattr(req, "user", None):
            user_id = getattr(req.user, "id", None)

        if not user_id:
            return

        payload = {
            "request_id": req.id,
            "status": req.status,
            "assigned_driver_id": getattr(req, "assigned_driver_id", None),
            "ts": int(timezone.now().timestamp() * 1000),
        }
        logger.warning(
            "[DELIVERY][WS][SEND] group=%s event=%s payload=%s",
            delivery_user_room(int(user_id)),
            WS_EVENT_REQUEST_STATUS,
            payload,
        )
        _ws_send_group(delivery_user_room(int(user_id)), WS_EVENT_REQUEST_STATUS, payload)
        _ws_send_group(user_room(int(user_id)), WS_EVENT_REQUEST_STATUS, payload)
    except Exception as e:
        logger.warning("[DELIVERY][WS] _broadcast_request_status_to_client error req=%s err=%s", getattr(req, "id", None), e)


# ─────────────────────────────────────────────
# Dispatch logic
# ─────────────────────────────────────────────
def driver_has_active_pickup_nearby(driver: DeliveryDriver, pickup_lat: float, pickup_lng: float) -> bool:
    qs = DeliveryRequest.objects.filter(
        assigned_driver=driver,
        status__in=ACTIVE_REQUEST_STATUSES,
        pickup_lat__isnull=False,
        pickup_lng__isnull=False,
    ).only("pickup_lat", "pickup_lng")

    for r in qs:
        dist = haversine_km(pickup_lat, pickup_lng, r.pickup_lat, r.pickup_lng)
        if dist <= CLUSTER_RADIUS_KM:
            return True
    return False


def create_offers_for_request(req: DeliveryRequest) -> int:
    """
    Mode A (GPS dispo): ville + véhicule + dispo + distance (15/20/30km)
    Mode B (GPS absent): ville + véhicule + dispo (sans distance)
    + WS temps réel: pool + ciblé à chaque offer créée
    """
    # VAN = opérateur humain
    if req.vehicle_type == DeliveryDriver.VEHICLE_VAN:
        logger.info("[DELIVERY][DISPATCH] req=%s van_service -> no auto dispatch", req.id)
        return 0

    drivers_qs = DeliveryDriver.objects.filter(
        is_active=True,
        is_available=True,
        city=req.city,
        vehicle_type=req.vehicle_type,
    )

    if not drivers_qs.exists():
        logger.info(
            "[DELIVERY][DISPATCH] req=%s city=%s vehicle=%s -> no eligible drivers",
            req.id, req.city, req.vehicle_type
        )
        return 0

    now = timezone.now()
    expires_at = now + timezone.timedelta(seconds=OFFER_TTL_SECONDS)

    already = set(DeliveryOffer.objects.filter(request=req).values_list("driver_id", flat=True))

    has_request_gps = (req.pickup_lat is not None and req.pickup_lng is not None)
    has_any_driver_gps = drivers_qs.filter(last_lat__isnull=False, last_lng__isnull=False).exists()
    can_do_gps_dispatch = has_request_gps and has_any_driver_gps

    logger.info(
        "[DELIVERY][DISPATCH] req=%s city=%s vehicle=%s mode=%s eligible=%s",
        req.id, req.city, req.vehicle_type,
        "A(GPS)" if can_do_gps_dispatch else "B(NO_GPS)",
        drivers_qs.count(),
    )

    created = 0

    # =========================
    # MODE A — GPS
    # =========================
    if can_do_gps_dispatch:
        candidates = []
        for d in drivers_qs:
            if d.id in already:
                continue
            if d.last_lat is None or d.last_lng is None:
                continue

            dist = haversine_km(req.pickup_lat, req.pickup_lng, d.last_lat, d.last_lng)
            score = float(dist)

            clustered = False
            if SMART_DISPATCH:
                try:
                    clustered = driver_has_active_pickup_nearby(d, req.pickup_lat, req.pickup_lng)
                except Exception:
                    clustered = False
                if clustered:
                    score -= 5.0

            candidates.append((d, dist, score, clustered))

        if not candidates:
            logger.info("[DELIVERY][DISPATCH] req=%s mode=A -> no candidates after gps filtering", req.id)
            return 0

        for radius in RADII:
            pool = [(d, dist, score, clustered) for (d, dist, score, clustered) in candidates if dist <= radius]
            pool.sort(key=lambda x: (x[2], x[1]))

            for d, dist, score, clustered in pool:
                if d.id in already:
                    continue

                offer = DeliveryOffer.objects.create(
                    request=req,
                    driver=d,
                    distance_km=float(dist),
                    expires_at=expires_at,
                )

                try:
                    logger.error("🔥 DISPATCH CALL FCM driver=%s user=%s request=%s", d.id, d.user_id, req.id)

                    logger.error("🧪 FCM DEBUG driver_user_id=%s", d.user_id)
                    logger.error("🧪 FCM DEBUG devices_all_user=%s",
                        list(Device.objects.filter(user_id=d.user_id).values("id","user_id","is_active","token")))
                    logger.error("🧪 FCM DEBUG devices_active=%s",
                                 list(Device.objects.filter(user_id=d.user_id, is_active=True).values("id","user_id","is_active","token")))
                    notify_delivery_driver_new_order(d, req)
                except Exception:
                    logger.exception("[FCM][DELIVERY] notify_delivery_driver_new_order crashed req=%s driver=%s", req.id, d.id)
                    pass

                _broadcast_offer_created(offer)

                logger.info(
                    "[DELIVERY][DISPATCH] req=%s -> offer=%s driver=%s dist=%.2f score=%.2f clustered=%s",
                    req.id, offer.id, d.id, float(dist), float(score), clustered
                )

                already.add(d.id)
                created += 1

            if created:
                logger.info("[DELIVERY][DISPATCH] req=%s mode=A -> created=%s within radius=%skm", req.id, created, radius)
                break

        return created

    # =========================
    # MODE B — NO GPS
    # =========================
    pool = drivers_qs.exclude(id__in=list(already)).order_by("updated_at", "id")

    for d in pool[:MAX_OFFERS_NO_GPS]:
        offer = DeliveryOffer.objects.create(
            request=req,
            driver=d,
            distance_km=0,
            expires_at=expires_at,
        )

        try:
            logger.error("🔥 DISPATCH CALL FCM driver=%s user=%s request=%s", d.id, d.user_id, req.id)

            logger.error("🧪 FCM DEBUG driver_user_id=%s", d.user_id)
            logger.error("🧪 FCM DEBUG devices_all_user=%s",
                         list(Device.objects.filter(user_id=d.user_id).values("id","user_id","is_active","token")))
            logger.error("🧪 FCM DEBUG devices_active=%s",
                         list(Device.objects.filter(user_id=d.user_id, is_active=True).values("id","user_id","is_active","token")))
            notify_delivery_driver_new_order(d, req)
        except Exception as e:
            logger.exception("[FCM][DELIVERY] notify_delivery_driver_new_order crashed req=%s driver=%s", req.id, d.id)

        _broadcast_offer_created(offer)
        created += 1

    logger.info("[DELIVERY][DISPATCH] req=%s mode=B -> created=%s (no gps)", req.id, created)
    return created


def expire_offers_and_request_if_needed(req: DeliveryRequest) -> None:
    if req.status != DeliveryRequest.STATUS_SEARCHING:
        return

    now = timezone.now()

    expired_ids = req.offers.filter(
        status=DeliveryOffer.STATUS_PENDING,
        expires_at__lte=now,
    ).values_list("id", flat=True)

    if expired_ids:
        req.offers.filter(id__in=list(expired_ids)).update(
            status=DeliveryOffer.STATUS_EXPIRED,
            responded_at=now,
        )

    has_pending = req.offers.filter(status=DeliveryOffer.STATUS_PENDING).exists()
    has_accepted = req.offers.filter(status=DeliveryOffer.STATUS_ACCEPTED).exists()

    if not has_pending and not has_accepted:
        req.status = DeliveryRequest.STATUS_FAILED
        req.save(update_fields=["status", "updated_at"])
        _broadcast_request_status_to_client(req)


def driver_active_deliveries_count(driver: DeliveryDriver) -> int:
    return DeliveryRequest.objects.filter(
        assigned_driver=driver,
        status__in=ACTIVE_REQUEST_STATUSES,
    ).count()


@transaction.atomic
def accept_delivery_offer(offer_id: int, user):
    """
    - offer -> ACCEPTED
    - request -> ASSIGNED
    - expire autres offers
    - notify client (FCM + WS)
    """
    now = timezone.now()

    offer = (
        DeliveryOffer.objects
        .select_for_update()
        .select_related("request", "driver")
        .get(id=offer_id)
    )

    req = DeliveryRequest.objects.select_for_update().get(id=offer.request_id)

    driver = DeliveryDriver.objects.select_for_update().get(id=offer.driver_id)

    # seul le livreur propriétaire
    if getattr(driver, "user_id", None) != getattr(user, "id", None):
        return {"ok": False, "detail": "Forbidden"}, 403

    if offer.status != DeliveryOffer.STATUS_PENDING:
        return {"ok": False, "detail": f"Offer not pending (status={offer.status})"}, 400

    if offer.expires_at and offer.expires_at <= now:
        offer.status = DeliveryOffer.STATUS_EXPIRED
        offer.responded_at = now
        offer.save(update_fields=["status", "responded_at"])
        expire_offers_and_request_if_needed(req)
        return {"ok": False, "detail": "Offer expired."}, 400

    if req.status != DeliveryRequest.STATUS_SEARCHING:
        return {"ok": False, "detail": f"Request not searchable ({req.status})."}, 400

    if req.assigned_driver_id is not None:
        return {"ok": False, "detail": "Request already assigned."}, 409

    active_count = driver_active_deliveries_count(driver)
    if active_count >= MAX_ACTIVE_DELIVERIES:
        if driver.is_available:
            driver.is_available = False
            driver.save(update_fields=["is_available", "updated_at"] if hasattr(driver, "updated_at") else ["is_available"])
        return {"ok": False, "detail": "Driver has reached max active deliveries (3)."}, 400

    if not driver.is_available or not driver.is_active:
        return {"ok": False, "detail": "Livreur indisponible."}, 400

    # assign request
    req.assigned_driver = driver
    req.status = DeliveryRequest.STATUS_ASSIGNED
    if hasattr(req, "assigned_by_operator"):
        req.assigned_by_operator = False
    req.save(update_fields=["assigned_driver", "status", "updated_at"] + (["assigned_by_operator"] if hasattr(req, "assigned_by_operator") else []))

    # accept offer
    offer.status = DeliveryOffer.STATUS_ACCEPTED
    offer.responded_at = now
    offer.save(update_fields=["status", "responded_at"])

    # expire others
    DeliveryOffer.objects.filter(
        request=req,
        status=DeliveryOffer.STATUS_PENDING,
    ).exclude(id=offer.id).update(
        status=DeliveryOffer.STATUS_EXPIRED,
        responded_at=now,
    )

    # driver availability
    new_active = active_count + 1
    must_be_available = new_active < MAX_ACTIVE_DELIVERIES
    if driver.is_available != must_be_available:
        driver.is_available = must_be_available
        driver.save(update_fields=["is_available", "updated_at"] if hasattr(driver, "updated_at") else ["is_available"])

    logger.info(
        "[DELIVERY][ACCEPT] offer=%s req=%s driver=%s active_before=%s active_after=%s driver_available=%s",
        offer.id, req.id, driver.id, active_count, new_active, driver.is_available
    )

    # notify client: FCM + WS
    try:
        notify_client_update(req.user, "delivery", req.id, req.status)
    except Exception:
        pass

    _broadcast_request_status_to_client(req)

    return {
        "ok": True,
        "request_id": req.id,
        "offer_id": offer.id,
        "driver_id": driver.id,
        "driver_user_id": getattr(driver, "user_id", None),
        "driver_available": driver.is_available,
        "active_deliveries": new_active,
        "request_status": req.status,
        "offer_status": offer.status,
    }, 200