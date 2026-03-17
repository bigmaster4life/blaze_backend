# mapsproxy/views.py
import os
import math
import requests
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods
from django.conf import settings
from .models import PlaceLabel
from django.utils.timezone import now
import json

GOOGLE_KEY = getattr(settings, "GOOGLE_KEY", "")

def _proxy(url, params):
    r = requests.get(url, params=params, timeout=15)
    return JsonResponse(r.json(), status=r.status_code, safe=False)

@require_GET
def directions(request):
    origin = request.GET.get('origin')
    destination = request.GET.get('destination')
    language = request.GET.get('language', 'fr')
    if not origin or not destination:
        return JsonResponse({'detail': 'origin et destination requis'}, status=400)
    url = 'https://maps.googleapis.com/maps/api/directions/json'
    params = {'origin': origin, 'destination': destination, 'key': GOOGLE_KEY, 'language': language, 'mode': 'driving'}
    return _proxy(url, params)

@require_GET
def geocode(request):
    latlng = request.GET.get('latlng')
    language = request.GET.get('language', 'fr')
    if not latlng:
        return JsonResponse({'detail': 'latlng requis'}, status=400)
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    params = {
        'latlng': latlng,
        'key': GOOGLE_KEY,
        'language': language
    }
    result_type = request.GET.get('result_type')
    location_type = request.GET.get('location_type')
    if result_type:
        params['result_type'] = result_type
    if location_type:
        params['location_type'] = location_type
    return _proxy(url, params)

@require_GET
def places(request):
    input_text = request.GET.get('input')
    language = request.GET.get('language', 'fr')
    if not input_text:
        return JsonResponse({'detail': 'input requis'}, status=400)
    url = 'https://maps.googleapis.com/maps/api/place/autocomplete/json'
    params = {'input': input_text, 'key': GOOGLE_KEY, 'language': language}

    components = request.GET.get('components')
    if components:
        params['components'] = components

    location = request.GET.get('location')
    if location:
        params['location'] = location
    
    radius = request.GET.get('radius')
    if radius:
        params['radius'] = radius
    return _proxy(url, params)

@require_GET
def place_details(request):
    place_id = request.GET.get('place_id')
    language = request.GET.get('language', 'fr')
    if not place_id:
        return JsonResponse({'detail': 'place_id requis'}, status=400)
    url = 'https://maps.googleapis.com/maps/api/place/details/json'
    params = {
        'place_id': place_id,
        'key': GOOGLE_KEY,
        'language': language,
        'fields': 'geometry,name,vicinity,formatted_address,address_components,types'
    }
    return _proxy(url, params)

@require_GET
def forward_geocode(request):
    address = request.GET.get('address')
    language = request.GET.get('language', 'fr')
    if not address:
        return JsonResponse({'detail': 'address requis'}, status=400)
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    params = {'address': address, 'key': GOOGLE_KEY, 'language': language}
    return _proxy(url, params)

@require_GET
def places_nearby(request):
    lat = request.GET.get('lat')
    lng = request.GET.get('lng')
    language = request.GET.get('language', 'fr')
    radius = request.GET.get('radius', '120')

    if not lat or not lng:
        return JsonResponse({'detail': 'lat et lng requis'}, status=400)

    url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
    params = {
        'location': f'{lat},{lng}',
        'radius': radius,
        'key': GOOGLE_KEY,
        'language': language
    }

    return _proxy(url, params)

def _distance_m(lat1, lng1, lat2, lng2):
    # Haversine (mètres)
    R = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def _best_db_label(lat, lng, radius_m=120, city=None):
    qs = PlaceLabel.objects.all()
    if city:
        qs = qs.filter(city=city)

    # préfiltre grossier (≈ 0.002° ~ 200m)
    qs = qs.filter(lat__gte=lat-0.003, lat__lte=lat+0.003, lng__gte=lng-0.003, lng__lte=lng+0.003)

    best = None
    best_d = 10**9
    for it in qs[:200]:
        d = _distance_m(lat, lng, it.lat, it.lng)
        if d <= radius_m and d < best_d:
            best = it
            best_d = d

    return best, (best_d if best else None)

@require_GET
def resolve_label(request):
    lat = request.GET.get('lat')
    lng = request.GET.get('lng')
    language = request.GET.get('language', 'fr')
    radius = int(request.GET.get('radius', '120'))
    city = request.GET.get('city')  # optionnel

    if not lat or not lng:
        return JsonResponse({'detail': 'lat et lng requis'}, status=400)

    lat = float(lat); lng = float(lng)

    # 1) BD (labels Be)
    GENERIC = {"libreville"}
    best, dist_m = _best_db_label(lat, lng, radius_m=radius, city=city)
    if best and best.label:
        lbl = best.label.strip().lower()
        city_l = (best.city or "").strip().lower()

        if lbl not in GENERIC and not (city_l and lbl == city_l):
            return JsonResponse(
                {
                    'label': best.label,
                    'source': 'be_db',
                    'distance_m': dist_m,
                },
                status=200,
            )

    # 2) Nearby Places (Google)
    url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
    params = {
        'location': f'{lat},{lng}',
        'radius': str(radius),
        'key': GOOGLE_KEY,
        'language': language
    }
    nearby = requests.get(url, params=params, timeout=15).json()
    if nearby.get('status') == 'OK' and nearby.get('results'):
        top = nearby['results'][0]
        name = (top.get('name') or '').strip()
        vic = (top.get('vicinity') or '').strip()
        label = name if name else vic
        if name and vic and vic.lower().find(name.lower()) == -1:
            label = f"{name}, {vic}"
        if label:
            return JsonResponse({'label': label, 'source': 'google_nearby'}, status=200)

    # 3) Reverse geocode (Google)
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    geo = requests.get(url, params={'latlng': f'{lat},{lng}', 'key': GOOGLE_KEY, 'language': language}, timeout=15).json()
    if geo.get('status') == 'OK' and geo.get('results'):
        label = geo['results'][0].get('formatted_address')
        if label and '+' in label:
            label = None
        if label:
            return JsonResponse({'label': label, 'source': 'google_geocode'}, status=200)

    return JsonResponse({'label': None, 'source': 'none'}, status=200)

@csrf_exempt
@require_http_methods(["POST"])
def save_label(request):
    try:
        body = request.body.decode('utf-8') if request.body else '{}'
        data = json.loads(body)
    except Exception:
        return JsonResponse({'detail': 'JSON invalide'}, status=400)

    label = (data.get('label') or '').strip()
    lat = data.get('lat')
    lng = data.get('lng')
    city = (data.get('city') or None)
    source = (data.get('source') or 'user').strip()

    if not label or lat is None or lng is None:
        return JsonResponse({'detail': 'label, lat, lng requis'}, status=400)

    lat = float(lat); lng = float(lng)

    # si déjà un label très proche, on met à jour (évite doublons)
    existing, dist_m = _best_db_label(lat, lng, radius_m=60, city=city)
    if existing:
        existing.label = label
        existing.city = city
        existing.source = source
        existing.updated_at = now()
        existing.save()
        return JsonResponse({'ok': True, 'id': existing.id, 'mode': 'updated'}, status=200)

    obj = PlaceLabel.objects.create(
        lat=lat, lng=lng, label=label, city=city, source=source, confidence=70
    )
    return JsonResponse({'ok': True, 'id': obj.id, 'mode': 'created'}, status=201)