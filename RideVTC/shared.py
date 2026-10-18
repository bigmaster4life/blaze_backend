"""Two-booking shared trips, road-based insertion and measured discounts."""
from decimal import Decimal
from datetime import timedelta
from itertools import permutations
import math
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied
from .models import Ride, RideVehicle, SharedTrip, SharedBooking, Payment
from .pricing import split_amount

TERMINAL = ('completed', 'cancelled', 'rejected')


def metres(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    value = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lng2-lng1)/2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


def stops_for(ride):
    return [{'ride_id': ride.pk, 'kind': kind, 'lat': lat, 'lng': lng, 'label': label}
            for kind, lat, lng, label in [('pickup', ride.pickup_lat, ride.pickup_lng, ride.pickup_location),
                                         ('dropoff', ride.dropoff_lat, ride.dropoff_lng, ride.dropoff_location)]]


def road_legs(origin, stops):
    if not stops:
        return []
    params = {'origin': f'{origin[0]},{origin[1]}', 'destination': f"{stops[-1]['lat']},{stops[-1]['lng']}",
              'mode': 'driving', 'key': settings.GOOGLE_KEY}
    if len(stops) > 1:
        params['waypoints'] = '|'.join(f"{s['lat']},{s['lng']}" for s in stops[:-1])
    try:
        response = requests.get('https://maps.googleapis.com/maps/api/directions/json', params=params, timeout=(2, 3))
        response.raise_for_status()
        body = response.json()
        legs = body['routes'][0]['legs'] if body.get('status') == 'OK' else []
        if len(legs) != len(stops):
            return None
        durations = [float(leg['duration']['value']) for leg in legs]
        return durations if all(math.isfinite(n) and n >= 0 for n in durations) else None
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None


def route_delays(order, legs, onboard):
    clock, pickups, durations, arrivals = 0, {rid: 0 for rid in onboard}, {}, {}
    for stop, duration in zip(order, legs):
        clock += duration
        rid = stop['ride_id']
        if stop['kind'] == 'pickup':
            pickups[rid] = clock
        else:
            durations[rid] = clock - pickups[rid]
            arrivals[rid] = clock
    return durations, arrivals


def capacity_ok(order, rides, capacity, require_shared=False):
    aboard = {r.pk for r in rides.values() if r.status == 'in_progress'}
    if sum(rides[i].passenger_count for i in aboard) > capacity:
        return False
    overlap = len(aboard) > 1
    for stop in order:
        rid = stop['ride_id']
        if stop['kind'] == 'pickup':
            if rid in aboard:
                return False
            aboard.add(rid)
        elif rid not in aboard:
            return False
        else:
            aboard.remove(rid)
        overlap = overlap or len(aboard) > 1
        if sum(rides[i].passenger_count for i in aboard) > capacity:
            return False
    return not aboard and (overlap or not require_shared)


def compatible_order(trip, ride):
    existing = {b.ride_id: b.ride for b in trip.bookings.select_related('ride__pricing') if b.ride.status not in TERMINAL}
    if not existing or trip.bookings.count() >= 2 or any(r.user_id == ride.user_id for r in existing.values()):
        return None
    origin = (trip.last_lat, trip.last_lng)
    if None in origin or not trip.last_location_at or (timezone.now()-trip.last_location_at).total_seconds() > 120:
        return None
    baseline = road_legs(origin, trip.stops)
    if baseline is None:
        return None
    onboard = {r.pk for r in existing.values() if r.status == 'in_progress'}
    base_durations, base_arrivals = route_delays(trip.stops, baseline, onboard)
    rides = {**existing, ride.pk: ride}
    orders = [list(order) for order in permutations(trip.stops + stops_for(ride))
              if capacity_ok(order, rides, trip.vehicle.passenger_capacity, require_shared=True)]
    def geometric(order):
        pts = [origin] + [(s['lat'], s['lng']) for s in order]
        return sum(metres(a,b) for a,b in zip(pts, pts[1:]))
    limit = getattr(settings, 'VTC_SHARED_MAX_DETOUR_SECONDS', 300)
    best, best_duration = None, float('inf')
    # Bound provider calls and latency; failure leaves B as a separate request.
    for order in sorted(orders, key=geometric)[:2]:
        legs = road_legs(origin, order)
        if legs is None:
            continue
        durations, arrivals = route_delays(order, legs, onboard)
        if any(arrivals[rid]-base_arrivals[rid] > limit or durations[rid]-base_durations[rid] > limit for rid in existing):
            continue
        pickup_wait = sum(legs[:next(i for i,s in enumerate(order) if s['ride_id']==ride.pk and s['kind']=='pickup')+1])
        if pickup_wait > limit or durations[ride.pk] > ride.pricing.quoted_duration_seconds + limit:
            continue
        if sum(legs) < best_duration:
            best, best_duration = order, sum(legs)
    return best


def emit(ride, event, extra=None):
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    from .utils.rooms import user_room, driver_room
    payload = {'requestId': ride.pk, 'driver': {'id': ride.driver_id}, **(extra or {})}
    def send():
        channel = get_channel_layer()
        if channel:
            for group in (user_room(ride.user_id), driver_room(ride.driver_id)):
                async_to_sync(channel.group_send)(group, {'type': 'evt', 'event': event, 'payload': payload})
    transaction.on_commit(send, robust=True)


def notify_accepted(ride):
    emit(ride, 'ride.accepted', {'ride': {'id': ride.pk, 'status': ride.status, 'price': str(ride.price),
        'pickup': {'lat': ride.pickup_lat, 'lng': ride.pickup_lng, 'label': ride.pickup_location},
        'dropoff': {'lat': ride.dropoff_lat, 'lng': ride.dropoff_lng, 'label': ride.dropoff_location}}})


def try_join(ride, trip_id=None):
    if not getattr(settings, 'VTC_SHARED_ENABLED', False) or ride.ride_mode != 'shared' or ride.status != 'pending' or (timezone.now()-ride.requested_at).total_seconds() > 60:
        return False
    candidates = SharedTrip.objects.filter(state='active', vehicle__shared_enabled=True, vehicle__category=ride.category, vehicle__available=True, driver__driver_profile__is_blocked=False)
    if trip_id is not None:
        candidates = candidates.filter(pk=trip_id)
    candidates = candidates.select_related('vehicle').order_by('-created_at')[:3]
    for candidate in candidates:
        if ride.driver_id not in (None, candidate.driver_id):
            continue
        if candidate.last_lat is None or metres((candidate.last_lat, candidate.last_lng), (ride.pickup_lat, ride.pickup_lng)) > 2000:
            continue
        version = candidate.version
        order = compatible_order(candidate, ride)
        if not order:
            break
        with transaction.atomic():
            trip = SharedTrip.objects.select_for_update().get(pk=candidate.pk)
            ride = Ride.objects.select_for_update().get(pk=ride.pk)
            if ride.status != 'pending' or SharedBooking.objects.filter(ride=ride).exists():
                return False
            current = list(SharedBooking.objects.select_for_update().filter(trip=trip).select_related('ride'))
            if trip.state != 'active' or trip.version != version or len(current) >= 2 or not trip.vehicle.shared_enabled or not trip.vehicle.available:
                continue
            if metres((trip.last_lat, trip.last_lng), (candidate.last_lat, candidate.last_lng)) > 100:
                continue
            if not capacity_ok(order, {**{b.ride_id:b.ride for b in current}, ride.pk:ride}, trip.vehicle.passenger_capacity):
                continue
            ride.driver_id = trip.driver_id
            ride.vehicle_id = trip.vehicle_id
            ride.status = 'accepted'
            ride.dispatch_status = 'accepted'
            ride.accepted_at = timezone.now()
            ride.save(update_fields=['driver','vehicle','status','accepted_at','dispatch_status'])
            SharedBooking.objects.create(trip=trip, ride=ride)
            trip.stops, trip.version = order, trip.version+1
            trip.save(update_fields=['stops','version'])
            notify_accepted(ride)
            emit(ride, 'ride.shared.updated')
            return True
    return False


@transaction.atomic
def accept_first(ride_id, driver):
    get_user_model().objects.select_for_update().get(pk=driver.pk)
    from drivers.models import Driver
    if not getattr(settings, 'VTC_SHARED_ENABLED', False) or not Driver.objects.filter(user=driver, is_blocked=False).exists():
        raise PermissionDenied()
    ride = Ride.objects.select_for_update().get(pk=ride_id)
    if ride.ride_mode != 'shared' or ride.status != 'pending' or ride.driver_id not in (None, driver.pk) or (timezone.now()-ride.requested_at).total_seconds() > 60:
        raise ValidationError('Cette offre est indisponible.')
    if Ride.objects.filter(driver=driver, status__in=['accepted','in_progress']).exists():
        raise ValidationError('Un trajet est déjà actif.')
    vehicle = RideVehicle.objects.select_for_update().filter(driver=driver, category=ride.category, shared_enabled=True, available=True).first()
    if not vehicle or vehicle.passenger_capacity < ride.passenger_count:
        raise ValidationError('Véhicule non disponible pour le partagé.')
    trip = SharedTrip.objects.create(driver=driver, vehicle=vehicle, stops=stops_for(ride),
        last_lat=vehicle.latitude, last_lng=vehicle.longitude, last_location_at=None)
    ride.driver, ride.vehicle, ride.status, ride.accepted_at = driver, vehicle, 'accepted', timezone.now()
    ride.dispatch_status = 'accepted'
    ride.save(update_fields=['driver','vehicle','status','accepted_at','dispatch_status'])
    SharedBooking.objects.create(trip=trip, ride=ride)
    notify_accepted(ride)
    return trip


def match_waiting(trip_id):
    trip = SharedTrip.objects.select_related('vehicle').get(pk=trip_id)
    if trip.state != 'active' or trip.bookings.count() >= 2 or trip.last_lat is None:
        return
    pending = Ride.objects.filter(ride_mode='shared', status='pending', category=trip.vehicle.category,
        requested_at__gte=timezone.now()-timedelta(seconds=60)).exclude(user_id=trip.driver_id)[:10]
    origin = (trip.last_lat, trip.last_lng)
    ordered = sorted(pending, key=lambda r: metres(origin,(r.pickup_lat,r.pickup_lng)))
    for ride in ordered[:1]:
        try_join(ride, trip_id=trip_id)


def record_position(trip, lat, lng):
    now = timezone.now()
    aboard = list(trip.bookings.select_for_update().filter(ride__status='in_progress'))
    if trip.last_location_at and trip.last_lat is not None:
        elapsed = (now-trip.last_location_at).total_seconds()
        distance = metres((trip.last_lat, trip.last_lng), (lat,lng))
        if 0 < elapsed <= 120 and distance <= 60 * elapsed:
            for booking in aboard:
                booking.travelled_m += distance
                if len(aboard) > 1:
                    booking.shared_m += distance
                booking.save(update_fields=['travelled_m','shared_m'])
    trip.last_lat, trip.last_lng, trip.last_location_at = lat, lng, now
    trip.save(update_fields=['last_lat','last_lng','last_location_at'])
    for booking in trip.bookings.select_related('ride').exclude(ride__status__in=TERMINAL):
        Ride.objects.filter(pk=booking.ride_id).update(driver_lat=lat, driver_lng=lng)
        emit(booking.ride, 'ride.driver.location', {'lat':lat,'lng':lng})


@transaction.atomic
def complete_stop(trip_id, driver, version, ride_id, kind):
    trip = SharedTrip.objects.select_for_update().get(pk=trip_id, driver=driver)
    if trip.state != 'active' or trip.version != version or not trip.stops or trip.stops[0]['ride_id'] != ride_id or trip.stops[0]['kind'] != kind:
        raise ValidationError('L’itinéraire a changé. Actualisez les arrêts.')
    stop = trip.stops[0]
    if trip.last_lat is None or not trip.last_location_at or (timezone.now()-trip.last_location_at).total_seconds()>60 or metres((trip.last_lat,trip.last_lng),(stop['lat'],stop['lng']))>200:
        raise ValidationError('Approchez-vous de l’arrêt avec une position GPS récente.')
    booking = SharedBooking.objects.select_for_update().get(trip=trip, ride_id=ride_id)
    ride = Ride.objects.select_for_update().get(pk=ride_id)
    if kind == 'pickup':
        if ride.status != 'accepted':
            raise ValidationError('Prise en charge indisponible.')
        ride.status, booking.pickup_at = 'in_progress', timezone.now()
        ride.save(update_fields=['status'])
        booking.save(update_fields=['pickup_at'])
        emit(ride, 'ride.started', {'stopCountdown':True})
    else:
        if ride.status != 'in_progress':
            raise ValidationError('Ce client n’est pas à bord.')
        ratio = min(1, max(0, booking.shared_m/booking.travelled_m)) if booking.travelled_m > 0 else 0
        booking.discount_rate = (Decimal(str(ratio))*Decimal('.20')).quantize(Decimal('.0001'))
        booking.dropoff_at = timezone.now()
        booking.save(update_fields=['discount_rate','dropoff_at'])
        amounts = split_amount(ride.pricing.reference_price, ride.pricing.commission_rate, booking.discount_rate)
        ride.final_price, ride.status, ride.completed_at = amounts['paid_amount'], 'completed', timezone.now()
        ride.save(update_fields=['final_price','status','completed_at'])
        Payment.objects.get_or_create(idempotency_key=f'cash-{ride.pk}', defaults={
            'ride':ride,'amount':ride.final_price,'wallet':'CASH','provider':'CASH','status':'SUCCESS',
            'meta':{'source':'shared_dropoff_cash','discount_rate':str(booking.discount_rate)}})
        emit(ride, 'ride.finished', {'final_price':str(ride.final_price),'base_price':str(ride.price), 'pause_fee':0, 'ride_mode':'shared', 'shared_discount_percent':float(booking.discount_rate*100)})
    trip.stops, trip.version = trip.stops[1:], trip.version+1
    if not trip.stops:
        trip.state = 'completed'
    trip.save(update_fields=['stops','version','state'])
    return trip


class _RetryCancellation(Exception):
    pass


def cancel_booking(ride_id, user, reason_data=None):
    if not Ride.objects.filter(pk=ride_id, ride_mode='shared').exists():
        return None
    # A pending reservation can acquire membership while cancellation waits
    # for its row lock. Roll back that lock before retrying trip -> ride order.
    for _ in range(3):
        try:
            with transaction.atomic():
                membership = SharedBooking.objects.filter(ride_id=ride_id).first()
                trip = SharedTrip.objects.select_for_update().get(pk=membership.trip_id) if membership else None
                ride = Ride.objects.select_for_update().get(pk=ride_id)
                if trip is None and SharedBooking.objects.filter(ride_id=ride_id).exists():
                    raise _RetryCancellation()
                if user.pk not in (ride.user_id, ride.driver_id) and not user.is_staff:
                    raise PermissionDenied()
                if ride.status in TERMINAL:
                    return ride
                if ride.status == 'in_progress':
                    raise ValidationError('Le client est à bord. Effectuez sa dépose avant de terminer.')
                if ride.payments.filter(status='SUCCESS').exists():
                    raise ValidationError('Un paiement est enregistré. Contactez l’assistance pour régler l’annulation.')
                before = ride.status
                if reason_data is not None:
                    from analytics.cancellations import parse_reason, record_cancellation
                    reason = parse_reason(reason_data, customer=ride.user_id == user.pk, status_before=before)
                ride.status = 'cancelled'
                ride.dispatch_status = 'cancelled'
                ride.save(update_fields=['status', 'dispatch_status'])
                if reason_data is not None:
                    record_cancellation(ride, user, before, reason)
                if trip:
                    trip.stops = [s for s in trip.stops if s['ride_id'] != ride.pk]
                    trip.version += 1
                    if not trip.stops:
                        trip.state = 'completed'
                    trip.save(update_fields=['stops','version','state'])
                if ride.driver_id:
                    emit(ride,'ride.cancelled')
                return ride
        except _RetryCancellation:
            continue
    raise ValidationError('Le trajet a changé. Réessayez l’annulation.')


def trip_data(trip):
    return {'id':trip.pk,'version':trip.version,'state':trip.state,'stops':trip.stops,
            'bookings':[{'ride_id':b.ride_id,'status':b.ride.status,'passenger_count':b.ride.passenger_count,
                'reference_price':str(b.ride.price),'final_price':str(b.ride.final_price) if b.ride.final_price is not None else None,
                'pickup':b.ride.pickup_location,'dropoff':b.ride.dropoff_location}
                for b in trip.bookings.select_related('ride')]}
