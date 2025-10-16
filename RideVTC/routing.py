# RideVTC/routing.py
from django.urls import re_path
from .consumers import AppConsumer, DriverConsumer

websocket_urlpatterns = [
    re_path(r"^ws/app/?$", AppConsumer.as_asgi()),
    re_path(r"^ws/rides/driver/(?P<driver_id>\d+)/?$", DriverConsumer.as_asgi()),
]