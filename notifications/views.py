from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from drivers.models import Driver
from Delivery.models import DeliveryDriver
from notifications.models import Device
from rest_framework.permissions import AllowAny
from notifications.fcm import send_fcm_message
from django.http import HttpResponse


from .serializers import RegisterDeviceSerializer
from .push import send_fcm_to_user, send_fcm_to_driver

class RegisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("token")
        platform = request.data.get("platform", Device.ANDROID)

        if not token:
            return Response({"detail": "token requis"}, status=400)

        device = Device.objects.filter(token=token).first()

        if device:
            device.user = request.user
            device.driver = None
            device.platform = platform
            device.is_active = True
            device.save(update_fields=["user", "driver", "platform", "is_active", "updated_at"])
        else:
            device = Device.objects.create(
                user=request.user,
                token=token,
                platform=platform,
                is_active=True,
            )
        return Response({"ok": True, "device_id": device.id})

@api_view(["POST"])
@permission_classes([IsAdminUser])  # pour tests; mets IsAuthenticated si tu préfères
def send_ride_arrived(request):
    """
    Test d'envoi "Votre chauffeur est arrivé"
    Body:
      { "user_id": 42, "ride_id": 123, "sound": "default" }
    ou
      { "driver_id": 7, "ride_id": 123 }
    """
    user_id   = request.data.get("user_id")
    driver_id = request.data.get("driver_id")
    ride_id   = request.data.get("ride_id")

    title = request.data.get("title") or "Votre chauffeur est arrivé"
    body  = request.data.get("body")  or "Il vous attend au point de départ."
    sound = request.data.get("sound") or "default"   # "notify" (Android) / "notify.caf" (iOS)

    data = {"type": "ride.arrived", "ride_id": str(ride_id or "")}

    if user_id:
        resp = send_fcm_to_user(int(user_id), title, body, data, sound=sound)
    elif driver_id:
        resp = send_fcm_to_driver(int(driver_id), title, body, data, sound=sound)
    else:
        return Response({"detail": "user_id ou driver_id requis"}, status=400)

    return Response({"ok": True, "fcm": resp}, status=200)

class RegisterFCMTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("fcm_token")

        if not token:
            return Response(
                {"detail": "fcm_token requis"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = request.user

        # 1️⃣ Toujours stocker sur CustomUser
        user.fcm_token = token
        user.save(update_fields=["fcm_token"])

        # 2️⃣ Si chauffeur VTC
        try:
            driver = Driver.objects.get(user=user)
            driver.fcm_token = token
            driver.save(update_fields=["fcm_token"])
        except Driver.DoesNotExist:
            pass

        # 3️⃣ Si livreur
        try:
            delivery_driver = DeliveryDriver.objects.get(user=user)
            delivery_driver.fcm_token = token
            delivery_driver.save(update_fields=["fcm_token"])
        except DeliveryDriver.DoesNotExist:
            pass

        return Response(
            {"success": True, "message": "FCM token enregistré"},
            status=status.HTTP_200_OK
        )

def test_push(request):
    device = Device.objects.filter(is_active=True).first()

    if not device:
        return HttpResponse("NO DEVICE")

    ok = send_fcm_message(device.token, {
        "notification": {
            "title": "TEST",
            "body": "FCM DIRECT"
        },
        "android": {"priority": "HIGH"}
    })

    return HttpResponse("SENT" if ok else "FAILED")