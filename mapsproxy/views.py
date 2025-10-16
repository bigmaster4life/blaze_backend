# mapsproxy/views.py
import os
import requests
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.conf import settings

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
    params = {'latlng': latlng, 'key': GOOGLE_KEY, 'language': language}
    return _proxy(url, params)

@require_GET
def places(request):
    input_text = request.GET.get('input')
    language = request.GET.get('language', 'fr')
    if not input_text:
        return JsonResponse({'detail': 'input requis'}, status=400)
    url = 'https://maps.googleapis.com/maps/api/place/autocomplete/json'
    params = {'input': input_text, 'key': GOOGLE_KEY, 'language': language}
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
        'fields': 'geometry,name,formatted_address'
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