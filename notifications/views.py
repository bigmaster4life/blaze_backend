from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes

from .serializers import RegisterDeviceSerializer
from .push import send_fcm_to_user, send_fcm_to_driver

class RegisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = RegisterDeviceSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        return Response({"ok": True, "device_id": obj.id}, status=status.HTTP_201_CREATED)

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