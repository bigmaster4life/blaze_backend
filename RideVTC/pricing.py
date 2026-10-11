"""Server-owned VTC quotes. Shared rides are deliberately not enabled yet."""
from decimal import Decimal, ROUND_HALF_UP
import math
import requests
from django.conf import settings
from django.core import signing
from rest_framework import serializers
from rest_framework.exceptions import APIException

VERSION = 'classic-v1'
RATES = {'eco': 400, 'clim': 500, 'vip': 650}
CAPS = {'eco': 4550, 'clim': 5950, 'vip': 9900}
SHORT = {'eco': (1000, 1500), 'clim': (1500, 2000), 'vip': (2500, 3000)}
SALT = 'ridevtc.quote.v1'

class RouteUnavailable(APIException):
    status_code = 503
    default_detail = 'Calcul du trajet indisponible. Réessayez.'


def fare(category, distance_km):
    km = Decimal(str(distance_km))
    if category not in RATES or not km.is_finite() or km <= 0:
        raise ValueError('Invalid category or distance')
    if km <= 2:
        return Decimal(SHORT[category][0])
    if km <= 5:
        return Decimal(SHORT[category][1])
    raw = min(Decimal(500) + km * RATES[category], Decimal(CAPS[category]))
    return (raw / 10).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * 10


def split_amount(amount, commission_rate, discount_rate=0):
    amount, commission_rate, discount_rate = map(lambda x: Decimal(str(x)), (amount, commission_rate, discount_rate))
    if not all(x.is_finite() for x in (amount, commission_rate, discount_rate)) or amount < 0 or not 0 <= commission_rate <= 1 or not 0 <= discount_rate <= Decimal('.20'):
        raise ValueError('Invalid allocation')
    paid = (amount * (1 - discount_rate)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    platform = (paid * commission_rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return {'paid_amount': paid, 'platform_amount': platform, 'driver_amount': paid - platform}


class QuoteInput(serializers.Serializer):
    pickup_lat = serializers.FloatField(min_value=-90, max_value=90)
    pickup_lng = serializers.FloatField(min_value=-180, max_value=180)
    dropoff_lat = serializers.FloatField(min_value=-90, max_value=90)
    dropoff_lng = serializers.FloatField(min_value=-180, max_value=180)
    category = serializers.ChoiceField(choices=list(RATES))

    def validate(self, attrs):
        if not all(math.isfinite(attrs[k]) for k in ('pickup_lat', 'pickup_lng', 'dropoff_lat', 'dropoff_lng')):
            raise serializers.ValidationError('Coordonnées invalides.')
        return attrs


def route_metrics(data):
    key = getattr(settings, 'GOOGLE_KEY', '')
    if not key:
        raise RouteUnavailable()
    try:
        response = requests.get('https://maps.googleapis.com/maps/api/directions/json', params={
            'origin': f"{data['pickup_lat']},{data['pickup_lng']}",
            'destination': f"{data['dropoff_lat']},{data['dropoff_lng']}",
            'mode': 'driving', 'key': key,
        }, timeout=15)
        response.raise_for_status()
        body = response.json()
        if body.get('status') != 'OK':
            raise ValueError('Route unavailable')
        legs = body['routes'][0]['legs']
        distance = sum(leg['distance']['value'] for leg in legs)
        duration = sum(leg['duration']['value'] for leg in legs)
        if not math.isfinite(distance) or not math.isfinite(duration) or distance <= 0 or duration <= 0:
            raise ValueError('Invalid metrics')
        return distance / 1000, duration
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        raise RouteUnavailable() from None


def make_quote(user_id, data, metrics=None):
    distance, duration = metrics if metrics is not None else route_metrics(data)
    price = fare(data['category'], distance)
    # Freeze the configured rate in the signed quote.
    rate = Decimal(str(getattr(settings, 'VTC_QUOTE_COMMISSION_RATE', '0.22')))
    allocation = split_amount(price, rate)
    payload = {**data, 'user_id': user_id, 'distance_km': distance,
               'duration_seconds': duration, 'price': str(price),
               'commission_rate': str(rate), 'pricing_version': VERSION}
    return {**payload, **{k: str(v) for k, v in allocation.items()},
            'expires_in': 300, 'base_fare': 500, 'rate_per_km': RATES[data['category']],
            'cap_per_ride': CAPS[data['category']],
            'pause_free_minutes': int(getattr(settings, 'PAUSE_FREE_SECONDS', 300)) // 60,
            'pause_fee_per_minute': int(getattr(settings, 'PAUSE_RATE_PER_MIN', 250)),
            'quote_token': signing.dumps(payload, salt=SALT)}


def read_quote(token, user_id, data):
    try:
        quote = signing.loads(token, salt=SALT, max_age=300)
        if quote['user_id'] != user_id or quote['pricing_version'] != VERSION:
            raise ValueError('Quote owner/version mismatch')
        for field in ('category', 'pickup_lat', 'pickup_lng', 'dropoff_lat', 'dropoff_lng'):
            if quote[field] != data.get(field):
                raise ValueError('Quote itinerary mismatch')
        return quote
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        raise serializers.ValidationError({'quote_token': 'Devis invalide ou expiré. Recalculez le trajet.'}) from None
