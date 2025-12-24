from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from .serializers import (
    RegisterSerializer, UserSerializer, CustomerProfileSerializer, PhoneLoginSerializer, PhoneOTPRequestSerializer, PhoneOTPVerifySerializer
)
from rest_framework import status, viewsets, permissions
from .models import CustomUser, CustomerProfile, EmailOTP
from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.generics import ListAPIView
from django.shortcuts import get_object_or_404
from rest_framework.parsers import MultiPartParser, FormParser
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drivers.models import Driver
from django.utils import timezone
from .utils import normalize_phone_gabon
from django.contrib.auth import get_user_model

User = get_user_model()

class IsOwnerProfile(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user

class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            return Response({"message": "Utilisateur créé avec succès"}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email')
        phone = request.data.get('phone')
        password = request.data.get('password')

        if not password:
            return Response({"detail": "password required"}, status=400)

        # 1️⃣ Auth par EMAIL si fourni
        if email:
            user = authenticate(request, email=email, password=password)

        # 2️⃣ Sinon auth par téléphone
        elif phone:
            try:
                user_obj = CustomUser.objects.get(phone_number=phone)
            except CustomUser.DoesNotExist:
                return Response({"detail": "Numéro introuvable"}, status=401)

            user = authenticate(request, email=user_obj.email, password=password)

        else:
            return Response(
                {"detail": "email or phone required"},
                status=400
            )

        if user is None:
            return Response({"detail": "Identifiants invalides"}, status=401)

        # JWT
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)

        # userType
        raw_type = getattr(user, "user_type", "").lower()
        if raw_type in ("driver", "chauffeur"):
            user_type = "driver"
        else:
            user_type = "customer"

        profile = CustomerProfile.objects.filter(user=user).first()

        first_name = ""
        last_name = ""
        photo_url = ""

        if profile:
            first_name = profile.first_name or ""
            last_name = profile.last_name or ""
            if profile.photo:
                try:
                    photo_url = profile.photo.url
                except:
                    photo_url = str(profile.photo)

        if not first_name:
            first_name = getattr(user, "first_name", "") or ""
        if not last_name:
            last_name = getattr(user, "last_name", "") or ""

        full_name = f"{first_name} {last_name}".strip()

        # Chauffeur (si il y en a un)
        driver_profile_obj = Driver.objects.filter(user=user).first()

        data = {
            "access": access_token,
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "phone_number": user.phone_number or "",
                "email": user.email or "",
                "userType": user_type,

                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "photo": photo_url,
                "photo_url": photo_url,

            },
        }

        if driver_profile_obj:
            data["driver_profile"] = {
                "id": driver_profile_obj.id,
                "category": driver_profile_obj.category,
                "vehicle_plate": driver_profile_obj.vehicle_plate,
            }

        return Response(data, status=200)

class UserListView(ListAPIView):
    queryset = CustomUser.objects.all()
    serializer_class = UserSerializer
    permission_classes = [AllowAny]


class CustomerProfileViewSet(viewsets.ModelViewSet):
  queryset = CustomerProfile.objects.all()
  serializer_class = CustomerProfileSerializer
  permission_classes = [permissions.IsAuthenticated]
  parser_classes = [MultiPartParser, FormParser]  # pour upload photo

  def get_queryset(self):
    # limite aux profils du user courant
    return CustomerProfile.objects.filter(user=self.request.user)

  def perform_create(self, serializer):
    serializer.save(user=self.request.user)

  @action(methods=["get", "put", "patch"], detail=False, url_path="me")
  def me(self, request):
    """
    GET    /api/profiles/me/     -> lire mon profil (404 si pas encore créé)
    PUT    /api/profiles/me/     -> remplacer (creation implicite si inexistant)
    PATCH  /api/profiles/me/     -> mise à jour partielle
    """
    try:
      profile = CustomerProfile.objects.get(user=request.user)
    except CustomerProfile.DoesNotExist:
      if request.method == "GET":
        return Response({"detail": "Profile not found."}, status=404)
      # création si on fait PUT/PATCH sans profil existant
      serializer = self.get_serializer(data=request.data)
      serializer.is_valid(raise_exception=True)
      serializer.save(user=request.user)
      return Response(serializer.data, status=201)

    partial = request.method == "PATCH"
    serializer = self.get_serializer(profile, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)
  
class PhoneLoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        print("DEBUG /login-phone payload ->", request.data)
        ser = PhoneLoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.save()  # retourne tokens + user
        return Response(data, status=status.HTTP_200_OK)

class RequestOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = PhoneOTPRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.save()  # {"phone_number": "...", "otp": "..."} si DEBUG; "SENT" sinon
        return Response({"message": "OTP envoyé", **data}, status=status.HTTP_200_OK)


class VerifyOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = PhoneOTPVerifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.save()  # tokens + user + is_new_user
        return Response(data, status=status.HTTP_200_OK)
    
class VerifyOTPEmailView(APIView):
    def post(self, request):
        email = request.data.get("email")
        otp = request.data.get("otp")

        if not email or not otp:
            return Response({"detail": "Email ou code manquant."},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            otp_obj = EmailOTP.objects.get(email=email, otp=otp)
        except EmailOTP.DoesNotExist:
            return Response({"detail": "Code OTP invalide."},
                            status=status.HTTP_400_BAD_REQUEST)

        if otp_obj.expires_at < timezone.now():
            return Response({"detail": "Code expiré."},
                            status=status.HTTP_400_BAD_REQUEST)

        # récupérer l'utilisateur
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"detail": "Utilisateur introuvable"},
                            status=status.HTTP_404_NOT_FOUND)

        # supprimer OTP pour éviter réutilisation
        otp_obj.delete()

        # créer un token JWT temporaire
        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        return Response({
            "access": access,
            "refresh": str(refresh),
            "user_id": user.id
        })
    
class SetPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Définit (ou change) le mot de passe.
        - Si l'utilisateur n'a jamais eu de mot de passe -> pas besoin de 'old_password'.
        - Sinon, 'old_password' requis et doit matcher.
        Body:
        {
          "new_password": "...",
          "confirm_password": "...",
          "old_password": ""  # optionnel si aucun mot de passe existant
        }
        """
        user = request.user
        new_password = (request.data.get("new_password") or "").strip()
        confirm_password = (request.data.get("confirm_password") or "").strip()
        current_password = request.data.get("current_password", "")

        if not new_password or not confirm_password:
            return Response({"detail": "Nouveau mot de passe requis."}, status=400)
        if new_password != confirm_password:
            return Response({"detail": "La confirmation ne correspond pas."}, status=400)
        
        no_real_password = (not user.has_usable_password()) or (not (user.password or "").strip())

        # S'il a déjà un pwd, on exige l'ancien
        if not no_real_password:
            if not current_password:
                return Response({"detail": "Ancien mot de passe requis."}, status=400)
            if not user.check_password(current_password):
                return Response({"detail": "Ancien mot de passe invalide."}, status=401)


        user.set_password(new_password)
        user.save(update_fields=["password"])
        return Response({"ok": True, "must_change_password": False}, status=200)
    
class RequestOTPEmailView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from .serializers import EmailOTPRequestSerializer
        ser = EmailOTPRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()

        return Response(
            {
                "detail": "OTP envoyé par email.",
                "phone_number": ser.validated_data["phone_number"],
                "expires_in": 300
            }, 
            status=200
        )

class CheckPhoneView(APIView):
    """
    POST /api/users/auth/check-phone/
    Body: { "phone_number": "+241074562847" }
    Réponse:
    {
      "exists": true/false,
      "has_password": true/false
    }
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        raw = request.data.get("phone_number", "")
        phone = normalize_phone_gabon(raw)

        if not phone:
            return Response(
                {"detail": "Numéro invalide."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            user = CustomUser.objects.get(phone_number=phone)
        except CustomUser.DoesNotExist:
            return Response(
                {"exists": False, "has_password": False},
                status=status.HTTP_200_OK,
            )

        has_password = user.has_usable_password() and bool((user.password or "").strip())

        return Response(
            {
                "exists": True,
                "has_password": has_password,
            },
            status=status.HTTP_200_OK,
        )
