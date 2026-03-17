from django.urls import path
from .views import RegisterDeviceView, send_ride_arrived, RegisterFCMTokenView, test_push

urlpatterns = [
    path("register-device/", RegisterDeviceView.as_view()),
    path("send/ride-arrived/", send_ride_arrived),
    path("register-token/", RegisterFCMTokenView.as_view(), name="register-fcm-token"),
    path("test-push/", test_push),
]