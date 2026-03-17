import os
import requests
from typing import Dict, List, Tuple, Any

# --- Polyline decoder (Google polyline algorithm) ---
def decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    index = 0
    lat = 0
    lng = 0
    coordinates = []

    while index < len(encoded):
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coordinates.append((lat / 1e5, lng / 1e5))

    return coordinates


def _as_route(points: List[Tuple[float, float]]) -> List[Dict[str, float]]:
    return [{"latitude": lat, "longitude": lng} for lat, lng in points]


def get_directions(pickup_lat: float, pickup_lng: float, dropoff_lat: float, dropoff_lng: float) -> Dict[str, Any]:
    provider = (os.getenv("DIRECTIONS_PROVIDER") or "mapbox").lower()

    if provider == "mapbox":
        return _mapbox(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)

    if provider == "osrm":
        return _osrm(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)

    if provider == "google":
        return _google(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)

    raise ValueError("Unsupported DIRECTIONS_PROVIDER")


def _mapbox(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) -> Dict[str, Any]:
    token = os.getenv("MAPBOX_ACCESS_TOKEN")
    if not token:
        raise ValueError("MAPBOX_ACCESS_TOKEN missing")

    # Mapbox expects lng,lat
    coords = f"{pickup_lng},{pickup_lat};{dropoff_lng},{dropoff_lat}"
    url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{coords}"
    params = {
        "access_token": token,
        "geometries": "geojson",
        "overview": "full",
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    routes = data.get("routes") or []
    if not routes:
        raise ValueError("No routes from Mapbox")

    best = routes[0]
    distance_m = int(best.get("distance") or 0)
    duration_s = int(best.get("duration") or 0)

    geometry = (best.get("geometry") or {}).get("coordinates") or []
    # geometry: [[lng,lat], ...]
    route = [{"latitude": lat, "longitude": lng} for lng, lat in geometry]

    return {"distance_m": distance_m, "duration_s": duration_s, "route": route, "provider": "mapbox"}


def _osrm(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) -> Dict[str, Any]:
    base = os.getenv("OSRM_BASE_URL")  # ex: https://router.project-osrm.org
    if not base:
        raise ValueError("OSRM_BASE_URL missing")

    coords = f"{pickup_lng},{pickup_lat};{dropoff_lng},{dropoff_lat}"
    url = f"{base.rstrip('/')}/route/v1/driving/{coords}"
    params = {"overview": "full", "geometries": "geojson"}

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    routes = data.get("routes") or []
    if not routes:
        raise ValueError("No routes from OSRM")

    best = routes[0]
    distance_m = int(best.get("distance") or 0)
    duration_s = int(best.get("duration") or 0)

    coords_list = ((best.get("geometry") or {}).get("coordinates")) or []
    route = [{"latitude": lat, "longitude": lng} for lng, lat in coords_list]

    return {"distance_m": distance_m, "duration_s": duration_s, "route": route, "provider": "osrm"}


def _google(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) -> Dict[str, Any]:
    key = os.getenv("PLACES_DIRECTIONS_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        raise ValueError("PLACES_DIRECTIONS_KEY (or GOOGLE_MAPS_API_KEY) missing")

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{pickup_lat},{pickup_lng}",
        "destination": f"{dropoff_lat},{dropoff_lng}",
        "key": key,
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    routes = data.get("routes") or []
    if not routes:
        raise ValueError("No routes from Google")

    leg = (routes[0].get("legs") or [{}])[0]
    distance_m = int(((leg.get("distance") or {}).get("value")) or 0)
    duration_s = int(((leg.get("duration") or {}).get("value")) or 0)

    poly = ((routes[0].get("overview_polyline") or {}).get("points")) or ""
    points = decode_polyline(poly)
    route = _as_route(points)

    return {"distance_m": distance_m, "duration_s": duration_s, "route": route, "provider": "google"}