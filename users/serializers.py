from rest_framework import serializers
from .models import (
    CustomUser
)
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from .models import CustomerProfile
from .utils import normalize_phone_gabon
from rest_framework_simplejwt.tokens import RefreshToken

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = [
            'id',
            'first_name',
            'last_name',
            'email',
            'user_type',
            'date_joined',
        ]
        read_only_fields = ['id', 'date_joined']

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'password', 'user_type', 'phone_number']
        extra_kwargs = {
            'phone_number': {'required': False, 'allow_blank': True, 'allow_null': True},
        }

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = CustomUser.objects.create_user(
            **validated_data
        )
        user.set_password(password)
        user.save()
        return user

class CustomerProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerProfile
        fields = "__all__"
        read_only_fields = ["user"]
        extra_kwargs = {
            "gender": {"required": False, "allow_blank": True},
            "first_name": {"required": False},
            "last_name": {"required": False},
        }


class PhoneLoginSerializer(serializers.Serializer):
    phone_number = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        raw_phone = attrs.get("phone_number", "")
        password = attrs.get("password", "")
        phone = normalize_phone_gabon(raw_phone)

        try:
            user = CustomUser.objects.get(phone_number=phone)
        except CustomUser.DoesNotExist:
            raise serializers.ValidationError({"detail": "Utilisateur introuvable."})

        # Si tu utilises authenticate(email=..., password=...), on peut bypasser :
        if not user.check_password(password):
            raise serializers.ValidationError({"detail": "Mot de passe invalide."})

        if not user.is_active:
            raise serializers.ValidationError({"detail": "Compte inactif."})

        attrs["user"] = user
        return attrs

    def create(self, validated_data):
        user = validated_data["user"]
        refresh = RefreshToken.for_user(user)
        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "email": user.email,
                "phone_number": user.phone_number,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "user_type": user.user_type,
            },
        }
