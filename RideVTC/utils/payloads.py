# RideVTC/utils/payloads.py
from __future__ import annotations
import time
import re
import requests
from typing import Optional, Dict, Any, Tuple
from django.conf import settings

# ─────────────────────────────────────────────────────────────
# Mini caches (clé normalisée)
# ─────────────────────────────────────────────────────────────
_REV_TTL = 3600  # 1h
_GEO_TTL = 3600  # 1h
_REV_CACHE: Dict[tuple, tuple[float, Optional[str]]] = {}
_GEO_CACHE: Dict[str, tuple[float, Optional[Tuple[float, float, str]]]] = {}

def _rev_key(lat: float, lng: float, lang: str) -> tuple:
    return (round(lat, 5), round(lng, 5), (lang or "fr")[:5])

def _geo_key(label: str, lang: str) -> str:
    return f"{(label or '').strip().lower()}|{(lang or 'fr')[:5]}"

def _cache_get_rev(lat: float, lng: float, lang: str) -> Optional[str]:
    k = _rev_key(lat, lng, lang)
    rec = _REV_CACHE.get(k)
    if not rec: return None
    exp, val = rec
    if time.time() > exp:
        _REV_CACHE.pop(k, None)
        return None
    return val

def _cache_set_rev(lat: float, lng: float, lang: str, val: Optional[str]) -> None:
    _REV_CACHE[_rev_key(lat, lng, lang)] = (time.time() + _REV_TTL, val)

def _cache_get_geo(label: str, lang: str) -> Optional[Tuple[float, float, str]]:
    k = _geo_key(label, lang)
    rec = _GEO_CACHE.get(k)
    if not rec: return None
    exp, val = rec
    if time.time() > exp:
        _GEO_CACHE.pop(k, None)
        return None
    return val

def _cache_set_geo(label: str, lang: str, val: Optional[Tuple[float, float, str]]) -> None:
    _GEO_CACHE[_geo_key(label, lang)] = (time.time() + _GEO_TTL, val)

# ─────────────────────────────────────────────────────────────
# Heuristiques
# ─────────────────────────────────────────────────────────────
_PLUSCODE_RE = re.compile(r"^[23456789CFGHJMPQRVWX]{2,8}\+[23456789CFGHJMPQRVWX]{2,8}", re.I)

def looks_like_plus_code(s: Optional[str]) -> bool:
    if not s: return False
    return bool(_PLUSCODE_RE.match(s.strip()))

def _is_placeholder(label: Optional[str]) -> bool:
    if not label: return True
    t = label.strip().lower()
    return t in {"votre position actuelle", "ma position", "current location"}

def _is_bad_label(label: Optional[str]) -> bool:
    return _is_placeholder(label) or looks_like_plus_code(label)

def _norm_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "": return None
        return float(v)
    except Exception:
        return None

def _format_price(p: Any) -> str:
    try:
        return str(p) if p is not None else ""
    except Exception:
        return ""

# ─────────────────────────────────────────────────────────────
# Google & Nominatim
# ─────────────────────────────────────────────────────────────
def _rev_google(lat: float, lng: float, lang: str) -> Optional[str]:
    key = getattr(settings, "GOOGLE_MAPS_API_KEY", None)
    if not key: return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lng}", "language": lang or "fr", "key": key},
            timeout=5,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("status") == "OK" and js.get("results"):
            return js["results"][0].get("formatted_address")
    except Exception:
        pass
    return None

def _rev_nominatim(lat: float, lng: float, lang: str) -> Optional[str]:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": f"{lat}", "lon": f"{lng}", "accept-language": lang or "fr", "addressdetails": 1},
            headers={"User-Agent": "RideVTC/1.0 (reverse-geocode)"},
            timeout=6,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("display_name"):
            return js["display_name"]
        addr = js.get("address") or {}
        parts = [addr.get("road"), addr.get("suburb"), addr.get("city") or addr.get("town") or addr.get("village"), addr.get("country")]
        parts = [p for p in parts if p]
        if parts: return ", ".join(parts)
    except Exception:
        pass
    return None

def _geocode_google(label: str, lang: str) -> Optional[Tuple[float, float, str]]:
    key = getattr(settings, "GOOGLE_MAPS_API_KEY", None)
    if not key: return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": label, "language": lang or "fr", "key": key},
            timeout=6,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("status") == "OK" and js.get("results"):
            res = js["results"][0]
            loc = res.get("geometry", {}).get("location") or {}
            lat, lng = loc.get("lat"), loc.get("lng")
            if lat is not None and lng is not None:
                return float(lat), float(lng), res.get("formatted_address") or label
    except Exception:
        pass
    return None

def _geocode_nominatim(label: str, lang: str) -> Optional[Tuple[float, float, str]]:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": label, "limit": 1, "accept-language": lang or "fr"},
            headers={"User-Agent": "RideVTC/1.0 (geocode)"},
            timeout=7,
        )
        r.raise_for_status()
        arr = r.json()
        if isinstance(arr, list) and arr:
            it = arr[0]
            lat = it.get("lat"); lon = it.get("lon")
            if lat is not None and lon is not None:
                disp = it.get("display_name") or label
                return float(lat), float(lon), disp
    except Exception:
        pass
    return None

def reverse_geocode(lat: Optional[float], lng: Optional[float], lang: str = "fr") -> Optional[str]:
    if lat is None or lng is None: return None
    cached = _cache_get_rev(lat, lng, lang)
    if cached is not None: return cached
    addr = _rev_google(lat, lng, lang) or _rev_nominatim(lat, lng, lang)
    _cache_set_rev(lat, lng, lang, addr)
    return addr

def geocode_label(label: str, lang: str = "fr") -> Optional[Tuple[float, float, str]]:
    if not label: return None
    c = _cache_get_geo(label, lang)
    if c is not None: return c
    # Google d’abord (gère très bien les plus codes), sinon Nominatim
    res = _geocode_google(label, lang) or _geocode_nominatim(label, lang)
    _cache_set_geo(label, lang, res)
    return res

# ─────────────────────────────────────────────────────────────
# Construction du payload chauffeur (avec enrichissement)
# ─────────────────────────────────────────────────────────────
def _best_triplet(label: Optional[str], lat: Optional[float], lng: Optional[float], lang: str) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """
    Renvoie (label, lat, lng) optimal :
      - si label est “mauvais” et lat/lng présents → reverse
      - si label est “mauvais” et lat/lng absents → GEOCODE du label (plus code, adresse textuelle)
      - sinon conserve tel quel
    """
    lab = (label or "").strip()
    if _is_bad_label(lab):
        if lat is not None and lng is not None:
            addr = reverse_geocode(lat, lng, lang)
            return (addr or lab, lat, lng)
        # pas de coords → essayer de géocoder le libellé (notamment plus code)
        g = geocode_label(lab, lang)
        if g:
            glat, glng, gaddr = g
            return (gaddr or lab, glat, glng)
        return (lab, lat, lng)
    # libellé correct → si coords manquantes, tenter de les récupérer quand même
    if (lat is None or lng is None) and lab:
        g = geocode_label(lab, lang)
        if g:
            glat, glng, gaddr = g
            # on garde le libellé d’origine si déjà “propre”, mais on remplit les coords
            return (lab, glat, glng)
    return (lab, lat, lng)

def build_ride_offer_payload(ride, category: str = "eco", area: str = "city-default", language: str = "fr") -> Dict[str, Any]:
    """
    Construit le payload 'ride.requested' le plus lisible possible :
      - essaie d’avoir label *et* coords pour pickup/dropoff
      - convertit les plus codes en adresses réelles si possible
      - évite “Votre position actuelle” en remplaçant par une vraie adresse quand on a des coords
    """
    lang = (language or "fr").split(",")[0].strip()[:5] or "fr"

    p_lat = _norm_float(getattr(ride, "pickup_lat", None))
    p_lng = _norm_float(getattr(ride, "pickup_lng", None))
    d_lat = _norm_float(getattr(ride, "dropoff_lat", None))
    d_lng = _norm_float(getattr(ride, "dropoff_lng", None))

    p_label_src = getattr(ride, "pickup_location", None)
    d_label_src = getattr(ride, "dropoff_location", None)

    p_label, p_lat, p_lng = _best_triplet(p_label_src, p_lat, p_lng, lang)
    d_label, d_lat, d_lng = _best_triplet(d_label_src, d_lat, d_lng, lang)

    payload = {
        "id": getattr(ride, "id", None),
        "pickup": {"label": p_label or "", "lat": p_lat, "lng": p_lng},
        "dropoff": {"label": d_label or "", "lat": d_lat, "lng": d_lng},
        "distance_km": float(getattr(ride, "distance_km", 0) or 0),
        "price": _format_price(getattr(ride, "price", "")),
        "category": (category or "eco"),
        # "area": area,  # si tu veux l’inclure
    }
    return payload