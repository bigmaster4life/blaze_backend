from rest_framework import serializers
from .models import Device

class RegisterDeviceSerializer(serializers.ModelSerializer):
    # optionnels → pour enregistrer explicitement côté serveur
    user_id = serializers.IntegerField(required=False)
    driver_id = serializers.IntegerField(required=False)

    class Meta:
        model = Device
        fields = ["token", "platform", "device_id", "user_id", "driver_id"]

    def validate(self, attrs):
        request = self.context.get("request")
        # Si rien n’est fourni, on déduit user_id depuis l’utilisateur authentifié
        if not attrs.get("user_id") and not attrs.get("driver_id"):
            if request and request.user and request.user.is_authenticated:
                attrs["user_id"] = request.user.id
        if not attrs.get("user_id") and not attrs.get("driver_id"):
            raise serializers.ValidationError("user_id ou driver_id requis.")
        return attrs

    def create(self, validated_data):
        token     = validated_data["token"]
        platform  = validated_data.get("platform", Device.ANDROID)
        device_id = validated_data.get("device_id")

        # cibles
        user_id   = validated_data.get("user_id")
        driver_id = validated_data.get("driver_id")

        defaults = {
            "platform": platform,
            "device_id": device_id,
            "user_id": user_id,
            "driver_id": driver_id,
            "is_active": True,
        }
        obj, _ = Device.objects.update_or_create(token=token, defaults=defaults)
        return obj