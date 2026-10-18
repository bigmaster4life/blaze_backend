"""Read-only operational snapshot. No customer identity or automatic sanctions."""
import math
import unicodedata
from datetime import timedelta
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from drivers.models import Driver
from RideVTC.models import DriverPresence, Ride, SharedTrip
from Delivery.models import DeliveryDriver, DeliveryRequest, CityChoices


class CanSupervise(permissions.BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u.is_authenticated and u.is_active and (u.is_superuser or (u.is_staff and u.user_type == 'manager_staff')))


def city_key(value):
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode().lower().strip()
    return value.replace('-', '_').replace(' ', '_') if value not in ('city-default', 'default', '') else 'unknown'


def coordinates(lat, lng):
    if lat is None or lng is None:
        return None
    lat, lng = float(lat), float(lng)
    return [lat, lng] if math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180 else None


def position(lat, lng, stamp, now):
    point = coordinates(lat, lng)
    return {'coordinates': point, 'updated_at': stamp, 'fresh': bool(point and stamp and timedelta(0) <= now-stamp <= timedelta(seconds=120))}


class SupervisionView(APIView):
    permission_classes = [CanSupervise]

    def get(self, request):
        now = timezone.now()
        # City catalog currently covers Gabon only; unknown cities have no inferred country.
        cities = dict(CityChoices.choices)
        def place(value):
            key = city_key(value)
            return {'city': key, 'city_label': cities.get(key, value if key != 'unknown' else 'Ville non renseignée'), 'country': 'GA' if key in cities else 'unknown'}

        rides = list(Ride.objects.filter(status__in=['pending', 'accepted', 'in_progress']).order_by('pk')[:2001])
        deliveries = list(DeliveryRequest.objects.filter(status__in=['pending', 'searching', 'operator_required', 'assigned', 'picked_up']).order_by('pk')[:2001])
        drivers = list(Driver.objects.filter(user__is_active=True).select_related('user').order_by('pk')[:2001])
        couriers = list(DeliveryDriver.objects.filter(user__is_active=True).order_by('pk')[:2001])
        truncated = any(len(items)>2000 for items in (rides, deliveries, drivers, couriers))
        rides, deliveries, drivers, couriers = (items[:2000] for items in (rides, deliveries, drivers, couriers))
        presences = {p.driver_id: p for p in DriverPresence.objects.filter(driver_id__in=[d.user_id for d in drivers])}
        trips = {t.driver_id: t for t in SharedTrip.objects.filter(driver_id__in=[d.user_id for d in drivers], state='active')}
        ride_by_driver = {}
        for r in rides:
            ride_by_driver.setdefault(r.driver_id, []).append(r)
        delivery_by_driver = {}
        for d in deliveries:
            delivery_by_driver.setdefault(d.assigned_driver_id, []).append(d)
        partners = []
        for d in drivers:
            p = presences.get(d.user_id)
            assigned = ride_by_driver.get(d.user_id, [])
            trip = trips.get(d.user_id)
            loc = position(p.lat if p else None, p.lng if p else None, p.location_updated_at if p else None, now)
            if trip and trip.last_location_at and (not p or not p.location_updated_at or trip.last_location_at > p.location_updated_at):
                loc = position(trip.last_lat, trip.last_lng, trip.last_location_at, now)
            online = bool(p and p.is_online and p.last_seen and now-p.last_seen <= timedelta(seconds=120))
            eligible = not d.is_blocked and d.onboarding_completed and d.status == 'approved'
            state = 'busy' if any(r.status == 'in_progress' for r in assigned) else 'pickup' if assigned else 'available' if online and eligible else 'unavailable'
            partners.append({'key': f'vtc:{d.pk}', 'id': d.pk, 'service': 'vtc', 'name': d.full_name, 'vehicle': d.vehicle_plate, **place((assigned[0].pickup_city if assigned else None) or (p.city if p else None)), 'state': state, 'online': online, 'position': loc, 'jobs': [f'vtc:{r.pk}' for r in assigned]})
        for d in couriers:
            assigned = delivery_by_driver.get(d.pk, [])
            loc = position(d.last_lat, d.last_lng, d.last_location_update, now)
            state = 'busy' if any(r.status == 'picked_up' for r in assigned) else 'pickup' if assigned else 'available' if d.is_available and d.is_active and d.is_verified and not d.is_blocked and loc['fresh'] else 'unavailable'
            partners.append({'key': f'delivery:{d.pk}', 'id': d.pk, 'service': 'delivery', 'name': d.full_name, 'vehicle': d.get_vehicle_type_display(), **place(d.city), 'state': state, 'online': bool(loc['fresh'] and d.is_active), 'position': loc, 'jobs': [f'delivery:{r.pk}' for r in assigned]})
        jobs = [{'key': f'vtc:{r.pk}', 'id': r.pk, 'service': 'vtc', 'status': r.status, **place(r.pickup_city), 'pickup_label': r.pickup_location, 'dropoff_label': r.dropoff_location, 'pickup': coordinates(r.pickup_lat, r.pickup_lng), 'dropoff': coordinates(r.dropoff_lat, r.dropoff_lng), 'created_at': r.requested_at} for r in rides]
        jobs += [{'key': f'delivery:{r.pk}', 'id': r.pk, 'service': 'delivery', 'status': r.status, **place(r.city), 'pickup_label': r.pickup_label, 'dropoff_label': r.dropoff_label, 'pickup': coordinates(r.pickup_lat, r.pickup_lng), 'dropoff': coordinates(r.dropoff_lat, r.dropoff_lng), 'created_at': r.created_at} for r in deliveries]
        response = Response({'generated_at': now, 'freshness_seconds': 120, 'truncated': truncated, 'partners': partners, 'jobs': jobs})
        response['Cache-Control'] = 'no-store, private'
        return response
