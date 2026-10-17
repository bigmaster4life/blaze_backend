# Delivery/utils/geo.py
import math

def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)

    a = (math.sin(dphi / 2) ** 2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlmb / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def valid_coordinates(lat, lng):
    try:
        return (lat is not None and lng is not None
                and math.isfinite(float(lat)) and math.isfinite(float(lng))
                and -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180)
    except (TypeError, ValueError):
        return False
