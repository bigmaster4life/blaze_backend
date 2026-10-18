"""GPS timestamps must describe a position sample, never a socket heartbeat."""
import math
from django.utils import timezone
from .models import DriverPresence


def record_position(user_id, lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return False
    DriverPresence.objects.update_or_create(driver_id=user_id, defaults={
        'lat': lat, 'lng': lng, 'location_updated_at': timezone.now(),
    })
    return True
