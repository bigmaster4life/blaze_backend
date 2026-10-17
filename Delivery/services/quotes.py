"""Short-lived server quotes, bound to the customer and billable route."""
import hashlib
import json
import math
from django.core import signing
from rest_framework.exceptions import ValidationError
from Delivery.models import DeliveryRequest
from Delivery.services.directions import get_directions
from Delivery.utils.geo import valid_coordinates

SALT = 'delivery.cash.quote.v1'
MAX_AGE = 300


def fingerprint(data):
    route = {key: data.get(key) for key in ('type', 'vehicle_type', 'pickup_zone', 'dropoff_zone', 'van_service')}
    for key in ('pickup_lat', 'pickup_lng', 'dropoff_lat', 'dropoff_lng'):
        value = data.get(key)
        route[key] = round(float(value), 6) if value is not None else None
    return hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()


def issue_quote(data, user):
    if not valid_coordinates(data.get('pickup_lat'), data.get('pickup_lng')) or not valid_coordinates(data.get('dropoff_lat'), data.get('dropoff_lng')):
        raise ValidationError({'detail': 'Sélectionnez deux positions valides avant de demander le tarif.'})
    if data['vehicle_type'] == 'van':
        distance, price = 0, None
    else:
        try:
            directions = get_directions(data['pickup_lat'], data['pickup_lng'], data['dropoff_lat'], data['dropoff_lng'])
            meters = float(directions['distance_m'])
            if not math.isfinite(meters) or meters < 0:
                raise ValueError('Invalid distance')
        except Exception as exc:
            raise ValidationError({'detail': 'Le tarif ne peut pas être calculé pour le moment. Réessayez.'}) from exc
        distance = meters / 1000
        draft_data = {key: value for key, value in data.items() if key not in ('quote_token', '_quote', 'distance_km')}
        draft = DeliveryRequest(**draft_data, user=user, distance_km=distance)
        price = draft.compute_estimated_price()
    payload = {'user_id': user.pk, 'route': fingerprint(data), 'distance_km': distance, 'price': price}
    return {'quote_token': signing.dumps(payload, salt=SALT, compress=True), 'delivery_price': price, 'distance_km': distance, 'expires_in': MAX_AGE, 'payment_method': 'cash', 'operator_quote_required': price is None}


def read_quote(token, data, user):
    try:
        quote = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        if quote['user_id'] != user.pk or quote['route'] != fingerprint(data):
            raise signing.BadSignature('Quote belongs to a different route or customer')
        return quote
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise ValidationError({'detail': 'Le tarif a expiré ou les adresses ont changé. Revenez au récapitulatif pour le recalculer.'}) from exc
