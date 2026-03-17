# mapsproxy/urls.py
from django.urls import path
from .views import directions, geocode, places, place_details, forward_geocode, places_nearby, resolve_label, save_label

urlpatterns = [
    path('directions/', directions, name='maps-directions'),
    path('geocode/', geocode, name='maps-geocode'),
    path('forward-geocode/', forward_geocode, name='maps-forward-geocode'),
    path('places/', places, name='maps-places'),
    path('place-details/', place_details, name='maps-place-details'),
    path('places-nearby/', places_nearby, name='places_nearby'),
    path('label/resolve/', resolve_label),
    path('label/save/', save_label),

]