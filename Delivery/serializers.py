# Delivery/serializers.py
from rest_framework import serializers
from .models import DeliveryRequest, DeliveryDriver, DeliveryOffer
from Delivery.utils.phone import normalize_gabon_phone

from django.db import transaction
from django.contrib.auth import get_user_model

User = get_user_model()


# ============================================================
# CLIENT — DELIVERY REQUEST
# ============================================================

class DeliveryOrderDetailsSerializer(serializers.Serializer):
    funding_mode = serializers.ChoiceField(choices=['client_funded', 'driver_advance'], required=False, default='client_funded')
    kind = serializers.ChoiceField(choices=['restaurant', 'shopping'])
    merchant_name = serializers.CharField(max_length=160)
    items = serializers.CharField(max_length=4000)
    purchase_mode = serializers.ChoiceField(choices=['prepaid', 'buy_for_me'])
    budget = serializers.IntegerField(min_value=1, max_value=1000000, required=False, allow_null=True)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if attrs['purchase_mode'] == 'buy_for_me' and not attrs.get('budget'):
            raise serializers.ValidationError({'budget': 'Précisez le budget maximal des achats.'})
        if attrs['purchase_mode'] == 'prepaid':
            attrs['budget'] = None
            attrs['funding_mode'] = 'client_funded'
        return attrs


class DeliveryRequestCreateSerializer(serializers.ModelSerializer):
    quote_token = serializers.CharField(required=False, write_only=True)
    payment_method = serializers.ChoiceField(choices=['cash'], required=False, default='cash')
    payment_payer = serializers.ChoiceField(choices=['sender', 'recipient'], required=False, default='sender')
    order_details = DeliveryOrderDetailsSerializer(required=False)
    class Meta:
        model = DeliveryRequest
        fields = [
            "id", "payment_method", "payment_payer", "quote_token",
            "type",
            "vehicle_type",

            "pickup_zone",
            "dropoff_zone",

            "pickup_label", "pickup_lat", "pickup_lng",
            "dropoff_label", "dropoff_lat", "dropoff_lng",
            "contact_name", "contact_phone", "note", "order_details",
            "van_service", "scheduled_for",
            "distance_km",
        ]

    def validate(self, attrs):
        vehicle_type = attrs.get("vehicle_type")
        if attrs.get("order_details") and attrs.get("type", "package") != "food":
            raise serializers.ValidationError({"order_details": "Ces détails concernent les repas et les courses."})

        if not attrs.get("pickup_zone"):
            raise serializers.ValidationError({"pickup_zone": "Obligatoire."})
        
        if not attrs.get("dropoff_zone"):
            raise serializers.ValidationError({"dropoff_zone": "Obligatoire."})

        if not attrs.get("pickup_label"):
            raise serializers.ValidationError({"pickup_label": "Obligatoire."})

        if not attrs.get("dropoff_label"):
            raise serializers.ValidationError({"dropoff_label": "Obligatoire."})

        if vehicle_type == DeliveryDriver.VEHICLE_VAN:
            if not attrs.get("van_service"):
                raise serializers.ValidationError({
                    "van_service": "Obligatoire (moving ou shipping)."
                })

        token = attrs.pop('quote_token', None)
        if self.context.get('require_quote') and not token:
            raise serializers.ValidationError({'detail': 'Veuillez confirmer le tarif depuis le récapitulatif.'})
        if token:
            from .services.quotes import read_quote
            attrs['_quote'] = read_quote(token, attrs, self.context['request'].user)
        return attrs

    def to_representation(self, instance):
        # Creation validates structured input; stored legacy/parcel details may be {}.
        # Use the same output contract as GET, including the saved dispatch status.
        return DeliveryRequestOutSerializer(instance, context=self.context).data

    def create(self, validated_data):
        user = self.context["request"].user
        quote = validated_data.pop('_quote', None)
        if quote:
            validated_data['distance_km'] = quote['distance_km']
            if quote['price'] is not None:
                validated_data['estimated_price'] = quote['price']
                validated_data['final_price'] = quote['price']

        # compat provisoire avec l'ancien champ city
        if not validated_data.get("city"):
            validated_data["city"] = validated_data.get("pickup_zone")

        req = DeliveryRequest.objects.create(user=user, **validated_data)

        if req.vehicle_type == DeliveryDriver.VEHICLE_VAN:
            req.status = DeliveryRequest.STATUS_OPERATOR_REQUIRED
            req.estimated_price = 0
        else:
            req.status = DeliveryRequest.STATUS_SEARCHING
            req.estimated_price = req.compute_estimated_price()

        req.save(update_fields=["status", "estimated_price", "updated_at"])
        return req


class DeliveryRequestOutSerializer(serializers.ModelSerializer):
    payment_allocation = serializers.SerializerMethodField()

    def get_payment_allocation(self, obj):
        from .models import DeliveryPaymentAllocation
        try:
            allocation = obj.payment_allocation
        except DeliveryPaymentAllocation.DoesNotExist:
            return None
        return {field: str(getattr(allocation, field)) for field in (
            'commission_rate', 'paid_amount', 'platform_amount', 'driver_amount', 'cash_commission_due'
        )}

    class Meta:
        model = DeliveryRequest
        fields = "__all__"


# ============================================================
# LIVREUR — OFFERS
# ============================================================

class DeliveryOfferOutSerializer(serializers.ModelSerializer):
    request_id = serializers.IntegerField(source="request.id", read_only=True)

    pickup_label = serializers.CharField(source="request.pickup_label", read_only=True)
    dropoff_label = serializers.CharField(source="request.dropoff_label", read_only=True)

    # ➕ COORDONNÉES PICKUP
    pickup_lat = serializers.FloatField(source="request.pickup_lat", read_only=True)
    pickup_lng = serializers.FloatField(source="request.pickup_lng", read_only=True)

    # ➕ COORDONNÉES DROPOFF
    dropoff_lat = serializers.FloatField(source="request.dropoff_lat", read_only=True)
    dropoff_lng = serializers.FloatField(source="request.dropoff_lng", read_only=True)

    pickup_zone = serializers.CharField(source="request.pickup_zone", read_only=True)
    dropoff_zone = serializers.CharField(source="request.dropoff_zone", read_only=True)

    vehicle_type = serializers.CharField(source="request.vehicle_type", read_only=True)
    city = serializers.CharField(source="request.city", read_only=True)
    estimated_price = serializers.IntegerField(source="request.estimated_price", read_only=True)

    contact_name = serializers.CharField(source="request.contact_name", read_only=True)
    contact_phone = serializers.CharField(source="request.contact_phone", read_only=True)
    note = serializers.CharField(source="request.note", read_only=True)
    order_details = serializers.JSONField(source="request.order_details", read_only=True)
    delivery_type = serializers.CharField(source="request.type", read_only=True)
    payment_method = serializers.CharField(source='request.payment_method', read_only=True)
    payment_payer = serializers.CharField(source='request.payment_payer', read_only=True)

    class Meta:
        model = DeliveryOffer
        fields = [
            "id", "payment_method", "payment_payer",
            "request_id",

            "pickup_zone",
            "dropoff_zone",

            "pickup_label",
            "pickup_lat",
            "pickup_lng",

            "dropoff_label",
            "dropoff_lat",
            "dropoff_lng",

            "vehicle_type",
            "city",
            "estimated_price",

            "contact_name",
            "contact_phone",
            "note",
            "order_details",
            "delivery_type",

            "distance_km",
            "status",
            "expires_at",
            "created_at",
        ]


# ============================================================
# LIVREUR — ONBOARDING
# ============================================================

class DeliveryDriverOnboardingSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryDriver
        fields = [
            "id",
            "full_name",
            "phone",
            "city",
            "vehicle_type",
            "identity_card",
            "driving_license",
            "photo",
        ]

    def validate(self, attrs):
        instance = self.instance

        identity_card = attrs.get("identity_card") or getattr(instance, "identity_card", None)
        driving_license = attrs.get("driving_license") or getattr(instance, "driving_license", None)
        photo = attrs.get("photo") or getattr(instance, "photo", None)

        if not identity_card:
            raise serializers.ValidationError({"identity_card": "Carte d'identité requise."})

        if not driving_license:
            raise serializers.ValidationError({"driving_license": "Permis requis."})

        if not photo:
            raise serializers.ValidationError({"photo": "Photo requise."})

        return attrs

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)

        instance.onboarding_completed = True
        instance.save()

        return instance


# ============================================================
# LIVREUR — PROFIL
# ============================================================

class DeliveryDriverProfileSerializer(serializers.ModelSerializer):
    settlement_summary = serializers.SerializerMethodField()

    def get_settlement_summary(self, obj):
        from .services.accounting import driver_summary
        return driver_summary(obj)

    email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = DeliveryDriver
        read_only_fields = ("total_deliveries", "total_earnings")
        fields = [
            "id",
            "email",
            "full_name",
            "phone",
            "city",
            "vehicle_type",
            "is_active",
            "is_available",
            "is_verified",
            "onboarding_completed",
            "total_deliveries",
            "total_earnings", "settlement_summary",
            "created_at",
        ]


# ============================================================
# LIVREUR — STATS
# ============================================================

class DeliveryDriverStatsSerializer(DeliveryDriverProfileSerializer):
    class Meta:
        model = DeliveryDriver
        fields = [
            "total_deliveries",
            "total_earnings", "settlement_summary",
        ]


# ============================================================
# LIVREUR — LOCATION
# ============================================================

class DeliveryDriverLocationUpdateSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lng = serializers.FloatField()

# ============================================================
# LIVREUR — FCM TOKEN
# ============================================================

class DeliveryDriverFCMTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryDriver
        fields = ["fcm_token"]

class DeliveryDriverCreateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=30)
    email = serializers.EmailField()
    city = serializers.ChoiceField(choices=DeliveryDriver._meta.get_field("city").choices)
    vehicle_type = serializers.ChoiceField(choices=DeliveryDriver.VEHICLE_CHOICES)

    def validate_phone(self, value):
        phone = normalize_gabon_phone(value)

        if User.objects.filter(phone_number=phone).exists():
            raise serializers.ValidationError("Ce numéro est déjà utilisé.")

        return phone
    
    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Cet email est déjà utilisé.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        phone = validated_data["phone"]
        full_name = validated_data["full_name"]
        email = validated_data["email"]

        # Split nom
        parts = full_name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        # Création user
        user = User.objects.create_user(
            email=email,
            first_name=first_name,
            last_name=last_name,
            user_type="chauffeur",  # ou "delivery_driver" si tu veux
            phone_number=phone,
        )

        # Création profil livreur
        delivery_driver = DeliveryDriver.objects.create(
            user=user,
            full_name=full_name,
            phone=phone,
            city=validated_data["city"],
            vehicle_type=validated_data["vehicle_type"],
        )

        return delivery_driver

class DeliveryDriverAdminListSerializer(serializers.ModelSerializer):
    email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = DeliveryDriver
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "city",
            "vehicle_type",
            "onboarding_completed",
            "is_active",
            "is_verified",
            "is_available",
            "is_blocked",
            "block_reason",
            "created_at",
        ]

class DeliveryDriverAdminDetailSerializer(DeliveryDriverProfileSerializer):
    email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = DeliveryDriver
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "city",
            "vehicle_type",

            "onboarding_completed",
            "is_active",
            "is_available",
            "is_verified",
            "is_blocked",
            "block_reason",

            # Docs (aperçu)
            "identity_card",
            "driving_license",
            "photo",

            # Stats
            "total_deliveries",
            "total_earnings", "settlement_summary",

            "created_at",
            "updated_at",
        ]

class DeliveryDirectionsInSerializer(serializers.Serializer):
    pickup_lat = serializers.FloatField()
    pickup_lng = serializers.FloatField()
    dropoff_lat = serializers.FloatField()
    dropoff_lng = serializers.FloatField()


class LatLngSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class DeliveryDirectionsOutSerializer(serializers.Serializer):
    distance_m = serializers.IntegerField()
    duration_s = serializers.IntegerField()
    route = LatLngSerializer(many=True)
    provider = serializers.CharField()