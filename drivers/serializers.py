# drivers/serializers.py
from rest_framework import serializers
from .models import Driver, DriverDocs

class DriverSerializer(serializers.ModelSerializer):
    class Meta:
        model = Driver
        fields = '__all__'

class InviteDriverSerializer(serializers.Serializer):
    # champs fournis par le front “Chauffeurs”
    phone = serializers.CharField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    vehicle_plate = serializers.CharField()
    category = serializers.ChoiceField(choices=Driver.CATEGORY_CHOICES)
    # optionnel: role (par défaut 'chauffeur')
    role = serializers.CharField(required=False, allow_blank=True)

class DriverStatusSerializer(serializers.Serializer):
    must_reset_password = serializers.BooleanField()
    onboarding_completed = serializers.BooleanField()
    category = serializers.ChoiceField(choices=Driver.CATEGORY_CHOICES)

class DriverOnboardingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Driver
        fields = [
            "full_name", "vehicle_plate", "category", 
            "license_file", "id_card_file", "insurance_file",
            "accepted_terms", "onboarding_completed"
        ]
        read_only_fields = ["onboarding_completed"]
    
    def validate(self, attrs):
        if not attrs.get("accepted_terms"):
            raise serializers.ValidationError("Vous devez accepter les CGU pour continuer.")
        return attrs

class DriverPresenceSerializer(serializers.Serializer):
    online = serializers.BooleanField()
    lat = serializers.FloatField(required=False, allow_null=True)
    lng = serializers.FloatField(required=False, allow_null=True)

class MockRideRequestSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    message = serializers.CharField()
    request = serializers.DictField(required=False)

class DriverDocsSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverDocs
        fields = [
            "license_file",
            "insurance_file",
            "registration_file",
            "id_card_file",
            "status",
            "note",
            "updated_at",
        ]
        read_only_fields = ["status", "note", "updated_at"]  # le staff met à jour ces champs

    # Optionnel: pour retourner des URLs absolues (pratique côté app)
    license_file = serializers.FileField(required=False, allow_null=True)
    insurance_file = serializers.FileField(required=False, allow_null=True)
    registration_file = serializers.FileField(required=False, allow_null=True)
    id_card_file = serializers.FileField(required=False, allow_null=True)

class DriverMeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    first_name = serializers.CharField(allow_null=True)
    last_name = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)
    photo_url = serializers.CharField(allow_null=True)
    category = serializers.CharField(allow_null=True)
    is_online = serializers.BooleanField()
    rating_avg = serializers.FloatField(required=False, allow_null=True)
    rides_done = serializers.IntegerField(required=False, allow_null=True)
    cancel_rate = serializers.FloatField(required=False, allow_null=True)

class EarningSummarySerializer(serializers.Serializer):
    today = serializers.IntegerField()
    week = serializers.IntegerField()
    month = serializers.IntegerField()
    currency = serializers.CharField()