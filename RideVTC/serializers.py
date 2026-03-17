# RideVTC/serializers.py

from rest_framework import serializers
from .models import RideVehicle, Ride, DriverNavEvent
import re

PLUSCODE_RX = re.compile(r"^[23456789CFGHJMPQRVWX]+\+[\dA-Z]{2,}.*$", re.IGNORECASE)

def is_generic_pickup(label: str) -> bool:
    if not label:
        return True
    l = label.strip().lower()
    return ("votre position actuelle" in l) or ("ma position" in l)

class RideVehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RideVehicle
        fields = "__all__"


class RideCreateSerializer(serializers.Serializer):
    pickup_location = serializers.CharField()
    dropoff_location = serializers.CharField()
    pickup_lat = serializers.FloatField(required=False)
    pickup_lng = serializers.FloatField(required=False)
    dropoff_lat = serializers.FloatField(required=False)
    dropoff_lng = serializers.FloatField(required=False)
    distance_km = serializers.FloatField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    # donnés “hors modèle”
    area = serializers.CharField(required=False, allow_blank=True)
    category = serializers.ChoiceField(choices=[('eco','eco'),('clim','clim'),('vip','vip')], required=False)
    pickup_city = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        pickup_label = (attrs.get("pickup_location") or "").strip()
        p_lat = attrs.get("pickup_lat")
        p_lng = attrs.get("pickup_lng")

        drop_label = (attrs.get("dropoff_location") or "").strip()
        d_lat = attrs.get("dropoff_lat")
        d_lng = attrs.get("dropoff_lng")

        # ✅ coords obligatoires si pickup générique ("Votre position actuelle", "Ma position", …)
        if is_generic_pickup(pickup_label):
            if p_lat is None or p_lng is None:
                raise serializers.ValidationError({
                    "pickup": "Coordonnées requises pour 'Votre position actuelle' (pickup_lat/pickup_lng)."
                })
        # (optionnel) si destination est un Plus Code, exiger aussi les coords:
        # if drop_label and PLUSCODE_RX.match(drop_label) and (d_lat is None or d_lng is None)
        #     raise serializers.ValidationError({
        #         "dropoff": "Coordonnées requises pour un Plus Code (dropoff_lat/dropoff_lng)."
        #     })
        

        return attrs

    def create(self, validated_data, **extra):
        """
        Crée et retourne un Ride à partir des données validées.
        On enlève les clés qui n’existent pas dans le modèle (area/category).
        On accepte aussi d’éventuels kwargs passés par .save(...)
        """
        request = self.context.get("request")
        user = getattr(request, "user", None)

        # clés non présentes dans le modèle
        validated_data.pop("area", None)
        validated_data.pop("category", None)

        # merge d’eventuels kwargs fournis par self.save(**extra)
        if extra:
            validated_data.update(extra)

        cat = validated_data.pop("category", None)
        if cat:
            validated_data["category"] = cat

        # si ton modèle Ride a un default pour status/accepted_at/completed_at, tu peux les omettre
        return Ride.objects.create(user=user, **validated_data)


class RideSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ride
        fields = "__all__"


class RideOutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ride
        fields = ["id", "status"]

class DriverLocationSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lng = serializers.FloatField()

class RateDriverSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True)

class DriverRatingCreateSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(allow_blank=True, required=False)

class DriverVehicleMeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RideVehicle
        fields = [
            'id','brand', 'model', 'plate', 'color', 'year',
            'insurance_valid_until', 'technical_valid_until',
            'category', 'city', 'latitude', 'longitude','available',
        ]
class DriverNavEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverNavEvent
        fields = "__all__"
        read_only_fields = ("driver", "created_at")

class OperatorRideSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()

    class Meta:
        model = Ride
        fields = [
            "id",
            "status",
            "category",
            "price",
            "final_price",
            "pickup_location",
            "dropoff_location",
            "pickup_lat",
            "pickup_lng",
            "dropoff_lat",
            "dropoff_lng",
            "created_at",
            "driver_id",
            "user_id",
            "customer_name",
            "customer_phone",
        ]

    def get_customer_name(self, obj):
        user = getattr(obj, "user", None)
        if not user:
            return None

        full_name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
        if full_name:
            return full_name

        return getattr(user, "email", None)

    def get_customer_phone(self, obj):
        user = getattr(obj, "user", None)
        if not user:
            return None
        return getattr(user, "phone_number", None)