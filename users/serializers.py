from rest_framework import serializers
from django.core.cache import cache
from django.conf import settings
from datetime import timedelta
from django.utils import timezone
from .models import (
    CustomUser
)
import random
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from .models import CustomerProfile
from .utils import normalize_phone_gabon
from rest_framework_simplejwt.tokens import RefreshToken

try:
    from RideVTC.models import RideVehicle  # adapte si ton modèle est ailleurs
except Exception:
    RideVehicle = None


def build_auth_payload(user: CustomUser) -> dict:
    """
    Construit la réponse d’auth standard :

    {
      "access": "...",
      "refresh": "...",
      "user": {
        "id": ...,
        "phone_number": "...",
        "email": "...",
        "userType": "driver" | "customer",
        "full_name": "..."
      },
      "driver_profile": { ... }   # seulement si chauffeur + profil dispo
    }
    """
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)

    raw_type = (getattr(user, "user_type", "") or "").lower()
    # On mappe 'client' -> 'customer' pour coller au front
    if raw_type in ("driver", "chauffeur"):
        user_type = "driver"
    elif raw_type in ("client", "customer"):
        user_type = "customer"
    else:
        # fallback : on considère que tout ce qui n'est pas 'driver' est 'customer'
        user_type = "customer"

    full_name = getattr(user, "full_name", "").strip()
    if not full_name:
        full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()

    data: dict = {
        "access": access_token,
        "refresh": str(refresh),
        "user": {
            "id": user.id,
            "phone_number": getattr(user, "phone_number", "") or "",
            "email": user.email or "",
            "userType": user_type,   # ⚠️ camelCase attendu par le front
            "full_name": full_name,
        },
    }

    # ⬇️ OPTIONNEL : profil chauffeur / véhicule principal
    if user_type == "driver" and RideVehicle is not None:
        try:
            vehicle = RideVehicle.objects.filter(driver=user).order_by("id").first()
        except Exception:
            vehicle = None

        if vehicle:
            data["driver_profile"] = {
                "id": vehicle.id,
                "category": getattr(vehicle, "category", None),
                "vehicle_plate": getattr(vehicle, "license_plate", None) or getattr(vehicle, "vehicle_plate", None),
            }

    return data

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

        if not user.check_password(password):
            raise serializers.ValidationError({"detail": "Mot de passe invalide."})

        if not user.is_active:
            raise serializers.ValidationError({"detail": "Compte inactif."})

        attrs["user"] = user
        return attrs

    def create(self, validated_data):
        """
        Utilise la fonction build_auth_payload pour renvoyer un format cohérent :
        {
          "access": "...",
          "refresh": "...",
          "user": {
            "id": ...,
            "phone_number": "...",
            "email": "...",
            "userType": "driver" | "customer",
            "full_name": "..."
          },
          "driver_profile": { ... }  # si applicable
        }
        """
        from .serializers import build_auth_payload  # import interne
        user = validated_data["user"]
        return build_auth_payload(user)
    
def send_sms(phone: str, message: str) -> None:
    """
    Envoi d'un SMS.
    Par défaut : mode DEBUG → print dans la console.
    En production : décommenter l'appel API Airtel.
    """

    # --------------------------------------------------------
    # 🔵 MODE DEBUG (actuel)
    # --------------------------------------------------------
    print(f"[SMS DEBUG] To={phone} :: {message}")

    # --------------------------------------------------------
    # 🔴 MODE PRODUCTION – API AIRTEL (COMMENTÉ POUR L'INSTANT)
    # --------------------------------------------------------
    """
    try:
        url = "https://api.airtel.com/.../sms"   # URL fournie par Airtel

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # "X-API-Key": settings.AIRTEL_API_KEY,        # clé API Airtel
            # "Authorization": f"Bearer {settings.AIRTEL_TOKEN}", 
        }

        payload = {
            "sender": "BLAZE",                     # ou shortcode Airtel
            "recipient": phone,                    # 241XXXXXXXX
            "message": message
        }

        response = requests.post(url, json=payload, headers=headers, timeout=10)

        # Log si Airtel retourne une erreur
        if response.status_code >= 400:
            print("[AIRTEL SMS ERROR]", response.status_code, response.text)

    except Exception as e:
        print("[AIRTEL SMS EXCEPTION]", str(e))
    """
    # --------------------------------------------------------
    # 🟢 FIN
    # --------------------------------------------------------


class PhoneOTPRequestSerializer(serializers.Serializer):
    phone_number = serializers.CharField()

    def validate_phone_number(self, raw):
        phone = normalize_phone_gabon(raw or "")
        if not phone:
            raise serializers.ValidationError("Numéro invalide.")
        return phone

    def create(self, validated_data):
        phone = validated_data["phone_number"]

        otp = f"{random.randint(0, 999999):06d}"
        cache.set(f"otp:{phone}", otp, timeout=300)

        msg = f"Votre code Blaze est : {otp}. Il est valide 5 minutes."

        # 👉 Envoi du SMS
        send_sms(phone, msg)

        return {"phone_number": phone, "otp": otp if settings.DEBUG else "SENT"}


class PhoneOTPVerifySerializer(serializers.Serializer):
    phone_number = serializers.CharField()
    otp = serializers.CharField()

    def validate(self, attrs):
        phone = normalize_phone_gabon(attrs.get("phone_number", ""))
        otp = (attrs.get("otp") or "").strip()
        if not phone:
            raise serializers.ValidationError({"phone_number": "Numéro invalide."})
        cached = cache.get(f"otp:{phone}")
        if not cached or cached != otp:
            raise serializers.ValidationError({"otp": "OTP invalide ou expiré."})
        attrs["phone_number"] = phone
        return attrs

    def create(self, validated_data):
        from .models import CustomUser

        phone = validated_data["phone_number"]
        fallback_email = f"{phone}@ghost.local"
        user, created = CustomUser.objects.get_or_create(
            phone_number=phone,
            defaults={
                "email": fallback_email,
                "first_name": "",
                "last_name": "",
                # ⚠️ IMPORTANT : pour le front, on parle de "customer", pas "client"
                "user_type": "customer",
                "is_active": True,
            },
        )

        must_change = (not user.has_usable_password()) or (not (user.password or "").strip())

        # on nettoie l’OTP utilisé
        cache.delete(f"otp:{phone}")

        # payload commun (access, refresh, user{...}, driver_profile? )
        base = build_auth_payload(user)

        # on ajoute les flags spécifiques OTP
        base["is_new_user"] = created
        base["must_change_password"] = must_change

        return base
    
class EmailOTPRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        value = value.strip().lower()
        if not value:
            raise serializers.ValidationError("Email requis.")
        return value

    def create(self, validated_data):
        from .models import CustomUser
        from django.core.mail import send_mail
        from django.core.cache import cache
        from django.conf import settings
        import random

        email = validated_data["email"]

        # ➤ retrouver l'utilisateur Blaze
        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            raise serializers.ValidationError({"email": "Aucun utilisateur avec cet email."})

        phone = user.phone_number
        if not phone:
            raise serializers.ValidationError({"email": "Cet utilisateur n’a pas de numéro lié."})

        # ➤ Génération OTP
        otp = f"{random.randint(0, 999999):06d}"
        cache.set(f"otp:{phone}", otp, timeout=300)  # 5 minutes

        # ➤ Envoi email
        subject = "Votre code Blaze"
        message = (
            f"Bonjour {user.full_name or ''},\n\n"
            f"Voici votre code OTP : {otp}\n"
            f"Il est valable pendant 5 minutes.\n\n"
            "— Équipe Blaze"
        )
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@blaze.com")

        send_mail(subject, message, from_email, [email], fail_silently=False)

        return {
            "email": email,
            "phone_number": phone,
            "expires_in": 300,
            "otp": otp if settings.DEBUG else "SENT"
        }
