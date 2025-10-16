# drivers/views.py
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.db.models import Sum, Q
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from django.utils.crypto import get_random_string

from rest_framework import status, permissions
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.decorators import api_view, permission_classes
from rest_framework_simplejwt.tokens import RefreshToken
from .utils import normalize_gabon_phone

from .models import Driver, DriverDocs
from .serializers import (
    DriverSerializer,
    InviteDriverSerializer,
    DriverStatusSerializer,
    DriverOnboardingSerializer,
    DriverPresenceSerializer,
    MockRideRequestSerializer,
    DriverDocsSerializer,
    EarningSummarySerializer,

)
from RideVTC.models import Payment
import datetime

User = get_user_model()


class DriverListCreateView(APIView):
    """
    GET: Liste des chauffeurs (admin)
    POST: Création directe d'un chauffeur (admin) — tu peux préférer InviteDriverView.
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        drivers = Driver.objects.select_related("user").all().order_by("-created_at")
        serializer = DriverSerializer(drivers, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = DriverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class DriverDetailView(APIView):
    permission_classes = [IsAdminUser]

    def delete(self, request, pk):
        driver = get_object_or_404(Driver, pk=pk)
        driver.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _split_name(full_name: str):
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


class InviteDriverView(APIView):
    """
    POST /api/drivers/invite/
    Body: { phone, email, full_name, vehicle_plate, category, role? }
    Perm: admin only
    """
    permission_classes = [IsAdminUser]

    def post(self, request):
        ser = InviteDriverSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # 0) Normaliser le téléphone (format stocké: 241XXXXXXXX)
        phone_input   = (ser.validated_data["phone"] or "").strip()
        phone_normal  = normalize_gabon_phone(phone_input)

        email         = ser.validated_data["email"].strip()
        full_name     = ser.validated_data["full_name"].strip()
        vehicle_plate = ser.validated_data["vehicle_plate"].strip()
        category      = ser.validated_data["category"]
        role          = ser.validated_data.get("role") or "chauffeur"

        first_name, last_name = _split_name(full_name)

        # 1) Créer/trouver l'utilisateur par phone_number (ton CustomUser n'a pas 'username')
        user, created = User.objects.get_or_create(
            phone_number=phone_normal,
            defaults={
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "is_active": True,
                "user_type": "chauffeur",  # on peut marquer le rôle côté user si tu veux
            },
        )

        # 2) Mettre à jour les infos de base si besoin
        changed = False
        if user.email != email:
            user.email = email
            changed = True
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if last_name and user.last_name != last_name:
            user.last_name = last_name
            changed = True
        if getattr(user, "user_type", None) != "chauffeur":
            # au cas où l'utilisateur existait déjà avec un autre type
            user.user_type = "chauffeur"
            changed = True

        # 3) Mot de passe temporaire (11 caractères)
        temp_password = get_random_string(length=11)
        user.set_password(temp_password)

        if settings.DEBUG:
            print(
                f"[DEBUG] Nouveau chauffeur : {full_name} "
                f"(phone saisie='{phone_input}', stocké='{phone_normal}') "
                f"→ mot de passe temporaire = {temp_password}"
            )

        # Sauvegarde user
        if changed:
            user.save()
        else:
            # set_password a déjà marqué l'instance "dirty"
            user.save()

        # 4) Profil Driver (lié à l'user)
        driver, _ = Driver.objects.get_or_create(user=user)
        driver.full_name = full_name
        driver.email = email
        driver.phone = phone_normal                 # on stocke le numéro normalisé
        driver.vehicle_plate = vehicle_plate
        driver.role = role
        driver.category = category
        driver.must_reset_password = True
        driver.onboarding_completed = False
        driver.save()

        # 5) E‑mail d’invitation — on communique l'identifiant EXACT à utiliser (241XXXXXXXX)
        subject = "Vos accès chauffeur Blaze"
        msg = (
            f"Bonjour {full_name},\n\n"
            "Votre accès chauffeur a été créé.\n"
            f"Identifiant (téléphone) : {phone_normal}\n"
            f"Mot de passe temporaire : {temp_password}\n\n"
            "À la première connexion dans l’app Chauffeur, vous devrez compléter votre profil "
            "et définir un nouveau mot de passe.\n\n"
            "— Équipe Blaze"
        )
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@blaze.app")
        try:
            send_mail(subject, msg, from_email, [email], fail_silently=False)
            sent_info = {"email_sent": True}
        except Exception as e:
            sent_info = {"email_sent": False, "email_error": str(e)}

        return Response(
            {"detail": "Chauffeur invité avec succès.", **sent_info},
            status=status.HTTP_201_CREATED,
        )


class DriverStatusView(APIView):
    """
    GET /api/drivers/me/status/  (JWT requis côté chauffeur)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Pas de profil chauffeur."}, status=status.HTTP_404_NOT_FOUND)

        ser = DriverStatusSerializer({
            "must_reset_password": driver.must_reset_password,
            "onboarding_completed": driver.onboarding_completed,
            "category": driver.category,
        })
        return Response(ser.data)

class DriverOnboardingView(APIView):
    """
    POST /api/drivers/me/onboarding/
    JWT chauffeur requis
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Pas de profil chauffeur."}, status=404)

        ser = DriverOnboardingSerializer(driver, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(onboarding_completed=True)

        # ⚠️ étape clé : forcer un reset du mot de passe après onboarding
        driver.must_reset_password = False
        driver.save(update_fields=["must_reset_password", "onboarding_completed"])

        return Response({"detail": "Onboarding terminé."}, status=200)
    
class CompleteOnboardingView(APIView):
    """
    PATCH /api/drivers/me/onboarding/
    Body: { license_file, id_card_file, new_password, accept_terms }
    JWT requis
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Pas de profil chauffeur."}, status=404)

        accept_terms = request.data.get("accept_terms")
        new_password = request.data.get("new_password")

        if not accept_terms:
            return Response({"detail": "Vous devez accepter les conditions générales."}, status=400)

        # upload fichiers
        license_file = request.data.get("license_file")
        id_card_file = request.data.get("id_card_file")

        if license_file:
            driver.license_file = license_file
        if id_card_file:
            driver.id_card_file = id_card_file

        driver.onboarding_completed = True
        driver.must_reset_password = False
        driver.save()

        if new_password:
            request.user.set_password(new_password)
            request.user.save()

        return Response({"detail": "Onboarding terminé avec succès."}, status=200)

class DriverPresenceView(APIView):
    """
    PATCH /api/drivers/me/presence/
    Body: { "online": true|false, "lat"?: float, "lng"?: float }
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Pas de profil chauffeur."}, status=404)

        ser = DriverPresenceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        online = ser.validated_data["online"]
        lat = ser.validated_data.get("lat")
        lng = ser.validated_data.get("lng")

        driver.is_online = online
        if lat is not None and lng is not None:
            driver.last_latitude = lat
            driver.last_longitude = lng
        driver.save()

        return Response({"detail": "Présence mise à jour.", "online": driver.is_online})


class MockRideRequestView(APIView):
    """
    POST /api/drivers/mock-request/
    Retourne une demande factice si le chauffeur est en ligne.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Pas de profil chauffeur."}, status=404)

        if not driver.is_online:
            return Response({"ok": False, "message": "Vous êtes hors-ligne."}, status=200)

        # Ici on pourrait filtrer par driver.category, distance, etc.
        payload = {
            "pickup": {"latitude": 0.392, "longitude": 9.457, "label": "Montagne Sainte"},
            "dropoff": {"latitude": 0.389, "longitude": 9.462, "label": "Quartier Centre"},
            "category": driver.category,
            "estimated_fare": 2500,
            "eta": "6 min"
        }
        ser = MockRideRequestSerializer({"ok": True, "message": "Nouvelle course", "request": payload})
        return Response(ser.data, status=200)
    
@api_view(["POST"])
@permission_classes([IsAdminUser])
def resend_invite(request, pk: int):
    """
    POST /api/drivers/<pk>/resend-invite/
    Régénère un mot de passe temporaire et renvoie l'email d'invitation.
    - Normalise le numéro au format 241XXXXXXXX
    - Synchronise driver.phone et user.phone_number
    """
    driver = get_object_or_404(Driver.objects.select_related("user"), pk=pk)
    user = driver.user
    if not user:
        return Response({"detail": "Ce chauffeur n'a pas d'utilisateur lié."}, status=400)

    # Normalisation du numéro
    phone_normal = normalize_gabon_phone(driver.phone or "")
    if phone_normal and phone_normal != driver.phone:
        driver.phone = phone_normal
        driver.save(update_fields=["phone"])

    # Aligne aussi le CustomUser
    if hasattr(user, "phone_number") and user.phone_number != phone_normal:
        user.phone_number = phone_normal

    # Nouveau mot de passe temporaire
    temp_password = get_random_string(length=11)
    user.set_password(temp_password)
    user.is_active = True
    # S’assure du type utilisateur
    if getattr(user, "user_type", None) != "chauffeur":
        user.user_type = "chauffeur"
    user.save()

    # Marque le driver en reset
    if driver.must_reset_password is False:
        driver.must_reset_password = True
        driver.save(update_fields=["must_reset_password"])

    # Email
    subject = "Vos nouveaux accès chauffeur Blaze"
    msg = (
        f"Bonjour {driver.full_name},\n\n"
        "Voici vos nouveaux accès chauffeur.\n"
        f"Identifiant (téléphone) : {phone_normal or driver.phone}\n"
        f"Mot de passe temporaire : {temp_password}\n\n"
        "Veuillez vous connecter dans l’app Chauffeur et compléter votre profil.\n\n"
        "— Équipe Blaze"
    )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@blaze.app")
    try:
        send_mail(subject, msg, from_email, [driver.email], fail_silently=False)
        sent_info = {"email_sent": True}
    except Exception as e:
        sent_info = {"email_sent": False, "email_error": str(e)}

    if settings.DEBUG:
        print(
            f"[DEBUG] Resend invite → {driver.full_name} "
            f"(phone={phone_normal or driver.phone}) temp_pwd={temp_password}"
        )

    return Response({"detail": "Invitation renvoyée.", **sent_info}, status=200)

class DriverLoginView(APIView):
    """
    POST /api/drivers/login/
    Body: { "phone": "...", "password": "..." }
    - "phone" peut contenir soit le phone_number, soit l'email.
    Retour: { access, refresh }
    """
    permission_classes = []

    def post(self, request):
        raw_ident = (request.data.get("phone") or "").strip()
        password = request.data.get("password") or ""

        if not raw_ident or not password:
            return Response({"detail": "Téléphone/email et mot de passe requis."},
                            status=status.HTTP_400_BAD_REQUEST)

        User = get_user_model()

        # 1) Essayer par phone_number si ce n'est pas un email
        user = None
        is_email = "@" in raw_ident
        try:
            if is_email:
                user = User.objects.get(email=raw_ident)
            else:
                user = User.objects.get(phone_number=raw_ident)
        except User.DoesNotExist:
            # 2) Fallback: si l’entrée n’était pas un email, tenter par email
            if not is_email:
                try:
                    user = User.objects.get(email=raw_ident)
                except User.DoesNotExist:
                    return Response({"detail": "Identifiants invalides."},
                                    status=status.HTTP_401_UNAUTHORIZED)
            else:
                return Response({"detail": "Identifiants invalides."},
                                status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response({"detail": "Compte inactif."}, status=status.HTTP_403_FORBIDDEN)

        if not user.check_password(password):
            return Response({"detail": "Identifiants invalides."}, status=status.HTTP_401_UNAUTHORIZED)

        # Doit être chauffeur
        try:
            _ = user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Aucun profil chauffeur associé."},
                            status=status.HTTP_403_FORBIDDEN)

        refresh = RefreshToken.for_user(user)
        return Response({"access": str(refresh.access_token), "refresh": str(refresh)},
                        status=status.HTTP_200_OK)
    
class DriverDocsMeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]  # nécessaire pour <input type="file">

    def get(self, request):
        docs, _ = DriverDocs.objects.get_or_create(user=request.user)
        ser = DriverDocsSerializer(docs, context={"request": request})
        return Response(ser.data)

    def patch(self, request):
        docs, _ = DriverDocs.objects.get_or_create(user=request.user)
        ser = DriverDocsSerializer(docs, data=request.data, partial=True, context={"request": request})
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    
class DriverEarningsSummary(APIView):
    """
    GET /driver/earnings/summary/
    Renvoie { today, week, month, currency } basé sur Payment(status=SUCCESS) des rides du chauffeur.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        now = timezone.now()

        # bornes "locales" (selon TIME_ZONE du projet)
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_week = start_today - datetime.timedelta(days=start_today.weekday())  # lundi 00:00
        start_month = start_today.replace(day=1)

        base_qs = Payment.objects.filter(
            status="SUCCESS",
            ride__driver=request.user,
        )

        def sum_between(start_dt):
            qs = base_qs.filter(updated_at__gte=start_dt)
            total = qs.aggregate(s=Sum("amount"))["s"] or 0
            return float(total)

        data = {
            "today": sum_between(start_today),
            "week": sum_between(start_week),
            "month": sum_between(start_month),
            "currency": "XAF",
        }
        return Response(EarningSummarySerializer(data).data, status=200)

