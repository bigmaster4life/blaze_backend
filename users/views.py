from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from .serializers import (
    RegisterSerializer, UserSerializer, CustomerProfileSerializer, PhoneLoginSerializer, PhoneOTPRequestSerializer, PhoneOTPVerifySerializer
)
from rest_framework import status, viewsets, permissions
from .models import CustomUser, CustomerProfile
from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.generics import ListAPIView
from django.shortcuts import get_object_or_404
from rest_framework.parsers import MultiPartParser, FormParser
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drivers.models import Driver

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
    """
    Login email + mot de passe (peut servir pour admin / backoffice).
    Réponse alignée avec le format mobile:

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
      "driver_profile": {
        "id": ...,
        "category": "...",
        "vehicle_plate": "..."
      }  # seulement si c'est un chauffeur
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        if not email or not password:
            return Response(
                {"detail": "email and password required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, email=email, password=password)
        if user is None:
            return Response(
                {'detail': 'Identifiants invalides'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # JWT
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)

        # userType: on mappe ton champ user_type -> "driver" | "customer"
        raw_type = getattr(user, "user_type", None) or getattr(user, "userType", None)
        raw_type = (raw_type or "").lower()
        if raw_type in ("driver", "chauffeur"):
            user_type = "driver"
        elif raw_type in ("customer", "client"):
            user_type = "customer"
        else:
            # fallback : si un profil driver existe, alors "driver"
            user_type = "driver" if Driver.objects.filter(user=user).exists() else "customer"

        # Profil chauffeur, si existant
        driver_profile = Driver.objects.filter(user=user).first()

        data = {
            "access": access_token,
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "phone_number": getattr(user, "phone_number", "") or "",
                "email": user.email or "",
                "userType": user_type,
                "full_name": getattr(user, "full_name", "") or (
                    f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
                ),
            },
        }

        if driver_profile:
            data["driver_profile"] = {
                "id": driver_profile.id,
                "category": getattr(driver_profile, "category", None),
                "vehicle_plate": getattr(driver_profile, "vehicle_plate", None),
            }

        return Response(data, status=status.HTTP_200_OK)

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
        data = ser.save()

        return Response(
            {
                "detail": "OTP envoyé par email.",
            }, 
            status=200
        )

