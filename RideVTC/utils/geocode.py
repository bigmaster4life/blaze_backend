# RideVTC/utils/geocode.py
import requests
from django.conf import settings

def reverse_geocode(lat: float, lng: float, lang: str = "fr") -> str | None:
    """
    Utilise l’API Google Maps Geocoding.
    Assure-toi que settings.GOOGLE_MAPS_API_KEY est défini.
    """
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "latlng": f"{lat},{lng}",
            "key": settings.GOOGLE_MAPS_API_KEY,
            "language": lang,
        }
        resp = requests.get(url, params=params, timeout=4)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("results"):
            return None
        return data["results"][0].get("formatted_address")
    except Exception:
        return None