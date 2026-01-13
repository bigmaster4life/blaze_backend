import math
import random
import logging
from typing import Iterable, List, Optional, Set

from django.utils import timezone
from django.conf import settings

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import Ride, RideVehicle, DriverPresence, DriverStats, DriverNavEvent
from .utils.rooms import driver_room

logger = logging.getLogger(__name__)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _get_online_driver_ids(timeout_s: int = 30) -> Set[int]:
    now = timezone.now()
    cutoff = now - timezone.timedelta(seconds=timeout_s)
    qs = DriverPresence.objects.filter(is_online=True, last_seen__gte=cutoff)
    return set(qs.values_list("driver_id", flat=True))


def _get_excluded_drivers_for_ride(ride_id: int) -> Set[int]:
    qs = DriverNavEvent.objects.filter(
        request_id=ride_id,
        event_type="reject",
    )
    return set(qs.values_list("driver_id", flat=True))


def _category_fallbacks(primary: str) -> List[str]:
    primary = (primary or "eco").lower()
    if primary == "eco":
        return ["eco", "clim", "vip"]
    if primary == "clim":
        return ["clim", "eco", "vip"]
    if primary == "vip":
        return ["vip", "clim", "eco"]
    return ["eco", "clim", "vip"]


# =====================================================================
# ★★★ FONCTION AVEC LOGS DE DIAGNOSTIC ULTRA COMPLETS ★★★
# =====================================================================
def _find_top_candidates(
    ride: Ride,
    category: str,
    max_km: float = 10.0,
    exclude_driver_ids: Optional[Iterable[int]] = None,
) -> List[int]:

    logger.info("=== [DISPATCH][DEBUG] START _find_top_candidates ===")
    logger.info(f"[DISPATCH][DEBUG] ride#{ride.id} category={category}")

    if ride.pickup_lat is None or ride.pickup_lng is None:
        logger.warning("[DISPATCH][DEBUG] Pickup coordinates missing.")
        return []

    exclude_set = set(exclude_driver_ids or [])

    # 1) CHAUFFEURS EN LIGNE
    online_ids = _get_online_driver_ids(
        timeout_s=int(getattr(settings, "DRIVER_PRESENCE_TIMEOUT_S", 30))
    )
    logger.info(f"[DISPATCH][DEBUG] STEP 1: drivers online = {len(online_ids)} → {online_ids}")

    if not online_ids:
        logger.info("[DISPATCH][DEBUG] No online drivers.")
        return []

    # 2) VEHICULES PAR CATÉGORIE
    vehicles = (
        RideVehicle.objects
        .select_related("driver")
        .filter(
            category=category,
            driver__isnull=False,
            driver_id__in=online_ids,
        )
        .exclude(driver_id__in=exclude_set)
    )

    logger.info(f"[DISPATCH][DEBUG] STEP 2: vehicles category='{category}' = {vehicles.count()}")
    logger.info(f"[DISPATCH][DEBUG] STEP 2 IDs = {[v.driver_id for v in vehicles]}")

    candidates = []
    pickup_lat = float(ride.pickup_lat)
    pickup_lng = float(ride.pickup_lng)

    # 3) FILTRE DISTANCE
    logger.info(f"[DISPATCH][DEBUG] STEP 3: Testing distance (max {max_km} km)")

    for v in vehicles:
        lat = getattr(v, "latitude", None)
        lng = getattr(v, "longitude", None)

        if lat is None or lng is None:
            logger.warning(f"[DISPATCH][DEBUG] driver#{v.driver_id} has NO GPS coords → skipped.")
            continue

        d_km = _haversine_km(pickup_lat, pickup_lng, float(lat), float(lng))
        logger.info(f"[DISPATCH][DEBUG] driver#{v.driver_id} distance = {d_km:.3f} km")

        if d_km <= max_km:
            candidates.append((v.driver_id, d_km))

    logger.info(f"[DISPATCH][DEBUG] STEP 3 result: {len(candidates)} candidates inside radius")

    # 4) SI VIDE → fallback par ville
    if not candidates:
        rider_city = getattr(ride, "pickup_city", "") or ""
        logger.info(f"[DISPATCH][DEBUG] No distance candidates. Fallback city='{rider_city}'")

        if rider_city:
            presences = DriverPresence.objects.filter(is_online=True, city__iexact=rider_city)
            fallback_ids = [d for d in presences.values_list("driver_id", flat=True) if d not in exclude_set]

            logger.info(f"[DISPATCH][DEBUG] CITY fallback drivers = {fallback_ids}")
            return fallback_ids[:3]

        return []

    # 5) NOTES / CLASSEMENT
    driver_ids = [c[0] for c in candidates]
    stats_map = {s.driver_id: s for s in DriverStats.objects.filter(driver_id__in=driver_ids)}

    rated = []
    new = []

    for driver_id, dist_km in candidates:
        st = stats_map.get(driver_id)
        if st and st.rating_count > 0:
            rated.append((driver_id, dist_km, float(st.rating_avg), int(st.rating_count)))
        else:
            new.append((driver_id, dist_km))

    rated.sort(key=lambda x: (-x[2], -x[3], x[1]))
    random.shuffle(new)
    new.sort(key=lambda x: x[1])

    ordered = [r[0] for r in rated] + [n[0] for n in new]

    logger.info(f"[DISPATCH][DEBUG] FINAL ORDERED = {ordered[:3]}")
    logger.info("=== [DISPATCH][DEBUG] END _find_top_candidates ===\n")

    return ordered[:3]


# =====================================================================
# PAYLOAD BOOST-READY (COMPATIBLE)
# =====================================================================
def _build_ride_payload(ride: Ride) -> dict:
    base_price = getattr(ride, "price", None)
    boost_amount = int(getattr(ride, "boost_amount", 0) or 0)

    try:
        effective_price = int(base_price or 0) + boost_amount
    except Exception:
        effective_price = base_price

    return {
        "id": ride.id,
        "category": getattr(ride, "category", "eco"),
        "pickup": {
            "label": getattr(ride, "pickup_location", None),
            "lat": getattr(ride, "pickup_lat", None),
            "lng": getattr(ride, "pickup_lng", None),
        },
        "dropoff": {
            "label": getattr(ride, "dropoff_location", None),
            "lat": getattr(ride, "dropoff_lat", None),
            "lng": getattr(ride, "dropoff_lng", None),
        },
        "distance_km": getattr(ride, "distance_km", None),

        "base_price": str(base_price) if base_price is not None else None,
        "boost_amount": boost_amount,
        "price": str(effective_price),
        "price_text": f"{int(effective_price)} FCFA" if effective_price else "",

        "pickup_label": getattr(ride, "pickup_location", None),
        "dropoff_label": getattr(ride, "dropoff_location", None),

        "status": getattr(ride, "status", "pending"),
        "dispatch_status": getattr(ride, "dispatch_status", "searching"),
        "dispatch_attempt": int(getattr(ride, "dispatch_attempt", 0) or 0),
        "max_radius_km_used": getattr(ride, "max_radius_km_used", None),
    }


# =====================================================================
# DISPATCH PRINCIPAL
# =====================================================================
def dispatch_ride_offer(
    ride_id: int,
    area: str = "city-default",
    preferred_category: Optional[str] = None,
    extra_exclude_driver_ids: Optional[Iterable[int]] = None,
) -> bool:

    logger.warning("DEBUG DISPATCH START → ride_id=%s area=%s", ride_id, area)

    try:
        ride = Ride.objects.get(id=ride_id)
    except Ride.DoesNotExist:
        logger.warning("[DISPATCH] ride %s not found", ride_id)
        return False

    logger.warning(
        "DEBUG DISPATCH RIDE LOADED → pickup_lat=%s pickup_lng=%s category=%s",
        ride.pickup_lat,
        ride.pickup_lng,
        ride.category
    )

    if getattr(ride, "status", None) != "pending":
        logger.info("[DISPATCH] ride %s status=%s → skip", ride.id, ride.status)
        return False

    if getattr(ride, "driver_id", None):
        logger.info("[DISPATCH] ride %s already assigned → skip", ride.id)
        return False

    if ride.pickup_lat is None or ride.pickup_lng is None:
        logger.info("[DISPATCH] ride %s missing pickup coords → skip", ride.id)
        return False

    exclude_ids = set(extra_exclude_driver_ids or [])
    exclude_ids |= _get_excluded_drivers_for_ride(ride.id)

    cat_sequence = _category_fallbacks(preferred_category or getattr(ride, "category", "eco"))

    radii = [10, 15, 25, 30]

    candidates = []
    used_category = None
    used_radius = None

    logger.warning(
        "[DISPATCH] ride_id=%s area=%s preferred_category=%s cat_sequence=%s radii=%s exclude_count=%s",
        ride.id, area, preferred_category, cat_sequence, radii, len(exclude_ids)
    )

    for r_km in radii:
        for cat in cat_sequence:
            cands = _find_top_candidates(
                ride=ride,
                category=cat,
                max_km=r_km,
                exclude_driver_ids=exclude_ids,
            )
            if cands:
                candidates = cands
                used_category = cat
                used_radius = r_km
                break
        if candidates:
            break

    if not candidates:
        logger.info("[DISPATCH] ride %s → no candidates after radii %s", ride.id, radii)

        try:
            ride.dispatch_status = "need_boost"
            ride.dispatch_attempt = int(getattr(ride, "dispatch_attempt", 0) or 0) + 1
            ride.max_radius_km_used = radii[-1]
            ride.save(update_fields=["dispatch_status", "dispatch_attempt", "max_radius_km_used"])
        except Exception:
            logger.exception("[DISPATCH] failed to update ride need_boost")

        return False

    target_driver_id = candidates[0]

    ch_layer = get_channel_layer()
    if not ch_layer:
        logger.warning("[DISPATCH] no channel_layer → cannot WS dispatch")
        return False

    try:
        ride.dispatch_status = "dispatched"
        ride.max_radius_km_used = used_radius
        ride.save(update_fields=["dispatch_status", "max_radius_km_used"])
    except Exception:
        logger.exception("[DISPATCH] failed to update dispatch state")

    ride_payload = _build_ride_payload(ride)
    if used_category:
        ride_payload["category"] = used_category

    message = {
        "type": "ride.requested",
        "ride": ride_payload,
    }

    try:
        async_to_sync(ch_layer.group_send)(driver_room(target_driver_id), message)
        logger.info(
            "[DISPATCH] ride %s → sent to driver.%s (cat=%s, radius=%s, area=%s)",
            ride.id, target_driver_id, used_category, used_radius, area
        )
        return True
    except Exception as e:
        logger.exception("[DISPATCH] WS send failed for ride %s → driver.%s : %s", ride.id, target_driver_id, e)
        return False