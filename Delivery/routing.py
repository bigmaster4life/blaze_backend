# Delivery/routing.py
from django.urls import re_path
from .consumers import DeliveryDriverConsumer, DeliveryClientConsumer

websocket_urlpatterns = [
    re_path(
        r"^ws/delivery/driver/(?P<driver_id>\d+)/?$",
        DeliveryDriverConsumer.as_asgi(),
    ),
    re_path(r"ws/delivery/client/(?P<user_id>\d+)/$", DeliveryClientConsumer.as_asgi()),
]