from django.urls import path
from .views import RegisterDeviceView, send_ride_arrived

urlpatterns = [
    path("register-device/", RegisterDeviceView.as_view()),
    path("send/ride-arrived/", send_ride_arrived),
]