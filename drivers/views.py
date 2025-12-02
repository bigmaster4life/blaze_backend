# drivers/views.py
from django.conf import settings
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.db.models import Sum, Q
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from django.utils.crypto import get_random_string

from rest_framework import status, permissions
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
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
from RideVTC.permissions import CanViewDriverProfile
import datetime
import logging, uuid
from users.serializers import build_auth_payload 

User = get_user_model()
logger = logging.getLogger(__name__)

def _split_name(full_name: str):
    """
    Découpe un nom complet en (first_name, last_name).
    - "Jean"        -> ("Jean", "")
    - "Jean Dupont" -> ("Jean", "Dupont")
    - "Jean Pierre Dupont" -> ("Jean", "Pierre Dupont")
    """
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _safe(s, n=1000):
    try:
        return (s or "")[:n]
    except Exception:
        return "<unprintable>"


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
    """
    - GET : accessible aux clients / drivers selon CanViewDriverProfile
    - DELETE : réservé aux admins
    """

    def get_permissions(self):
        """
        Permissions différentes selon la méthode :
        - DELETE → admin only
        - GET → user connecté + logique CanViewDriverProfile
        """
        if self.request.method == "DELETE":
            permission_classes = [IsAdminUser]
        else:  # GET (et autres si tu en ajoutes plus tard)
            permission_classes = [IsAuthenticated, CanViewDriverProfile]
        return [p() for p in permission_classes]

    def get(self, request, pk):
        driver = get_object_or_404(Driver, pk=pk)
        # Très important pour déclencher has_object_permission()
        self.check_object_permissions(request, driver)

        serializer = DriverSerializer(driver)
        return Response(serializer.data)
    
    def patch(self, request, pk):
        driver = get_object_or_404(Driver, pk=pk)
        self.check_object_permissions(request, driver)
        is_blocked = request.data.get("is_blocked", None)
        block_reason = request.data.get("block_reason", "")

        if is_blocked is not None:
            driver.is_blocked = bool(is_blocked)
        if block_reason is not None:
            driver.block_reason = block_reason
        
        driver.save()
        return Response(DriverSerializer(driver).data, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        driver = get_object_or_404(Driver, pk=pk)
        # On applique aussi les permissions objet pour DELETE (admin)
        self.check_object_permissions(request, driver)

        driver.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InviteDriverView(APIView):
    """
    POST /api/drivers/invite/
    Body: { phone, email, full_name, vehicle_plate, category, role? }
    Admin only
    """
    permission_classes = [IsAdminUser]
    parser_classes = [JSONParser]  # force le parsing JSON (évite les surprises navigateur)

    def post(self, request):
        cid = uuid.uuid4().hex[:12]  # correlation ID pour les logs/retours

        # --- LOGS AVANT PARSING ---
        try:
            raw = request.body.decode("utf-8")
        except Exception:
            raw = "<unreadable>"
        logger.info(
            "[invite %s] start origin=%s ctype=%s len=%s raw=%s",
            cid,
            request.META.get("HTTP_ORIGIN"),
            request.content_type,
            request.META.get("CONTENT_LENGTH"),
            (raw[:800] if raw else ""),
        )

        # 1) Validation du payload → toujours répondre en JSON (jamais une page HTML 500)
        try:
            ser = InviteDriverSerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            logger.info("[invite %s] validated=%s", cid, ser.validated_data)
        except Exception as e:
            logger.exception("[invite %s] validation error", cid)
            resp = Response(
                {"detail": "Payload invalide.", "error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
            resp["X-Request-ID"] = cid
            return resp

        # 2) Création/MAJ atomique et blindée (aucune exception ne doit sortir en HTML)
        try:
            with transaction.atomic():
                # Normaliser le téléphone (format stocké: 241XXXXXXXX)
                phone_input = (ser.validated_data["phone"] or "").strip()
                phone_normal = normalize_gabon_phone(phone_input)

                email = ser.validated_data["email"].strip()
                full_name = ser.validated_data["full_name"].strip()
                vehicle_plate = ser.validated_data["vehicle_plate"].strip()
                category = ser.validated_data["category"]
                role = ser.validated_data.get("role") or "chauffeur"

                # A) Validation téléphone — évite "241" seul
                # 241 + 8 chiffres (GA) => 11 caractères minimum
                if not phone_normal or len(phone_normal) < 11:
                    resp = Response(
                        {"detail": "Numéro de téléphone invalide.", "phone": phone_input, "normalized": phone_normal},
                        status=400,
                    )
                    resp["X-Request-ID"] = cid
                    return resp

                first_name, last_name = _split_name(full_name)

                # B) Pré-vérifs de doublons lisibles (avant assignations/sauvegardes)
                #  - email déjà utilisé par un autre user (autre phone_number)
                if User.objects.filter(email=email).exclude(phone_number=phone_normal).exists():
                    resp = Response(
                        {"detail": "Cet email est déjà utilisé par un autre utilisateur."},
                        status=400,
                    )
                    resp["X-Request-ID"] = cid
                    return resp

                #  - email/phone/plate déjà utilisés côté Driver
                if Driver.objects.filter(email=email).exists():
                    resp = Response({"detail": "Cet email est déjà utilisé pour un chauffeur."}, status=400)
                    resp["X-Request-ID"] = cid
                    return resp

                if Driver.objects.filter(phone=phone_normal).exists():
                    resp = Response({"detail": "Ce téléphone est déjà utilisé pour un chauffeur."}, status=400)
                    resp["X-Request-ID"] = cid
                    return resp

                if vehicle_plate and Driver.objects.filter(vehicle_plate=vehicle_plate).exists():
                    resp = Response({"detail": "Cette plaque est déjà utilisée."}, status=400)
                    resp["X-Request-ID"] = cid
                    return resp

                # C) Créer/trouver l'utilisateur par phone_number
                user, _created = User.objects.get_or_create(
                    phone_number=phone_normal,
                    defaults={
                        "email": email,
                        "first_name": first_name,
                        "last_name": last_name,
                        "is_active": True,
                        "user_type": "chauffeur",
                    },
                )

                # D) MAJ des infos si besoin
                changed = False
                if user.email != email:
                    user.email = email; changed = True
                if first_name and user.first_name != first_name:
                    user.first_name = first_name; changed = True
                if last_name and user.last_name != last_name:
                    user.last_name = last_name; changed = True
                if getattr(user, "user_type", None) != "chauffeur":
                    user.user_type = "chauffeur"; changed = True

                # E) Mot de passe temporaire (11 caractères)
                temp_password = get_random_string(length=11)
                user.set_password(temp_password)
                user.save()  # set_password marque l'instance "dirty"

                # F) Profil Driver (lié à l'user) — sauve de façon protégée
                driver, _ = Driver.objects.get_or_create(user=user)
                driver.full_name = full_name
                driver.email = email
                driver.phone = phone_normal
                driver.vehicle_plate = vehicle_plate
                driver.role = role
                driver.category = category
                driver.must_reset_password = True
                driver.onboarding_completed = False

                try:
                    driver.save()
                except IntegrityError as ie:
                    # Si tu as des contraintes unique_together/UniqueConstraint en DB
                    logger.exception("[invite %s] integrity error on driver.save()", cid)
                    resp = Response(
                        {"detail": "Conflit de données (doublon en base).", "error": str(ie)},
                        status=400,
                    )
                    resp["X-Request-ID"] = cid
                    return resp

        except Exception as e:
            # Ici on capture TOUT (intégrité, utilitaire, etc.) → JSON 500 propre
            logger.exception("[invite %s] unexpected error during create/update", cid)
            resp = Response(
                {"detail": "Erreur serveur pendant l’invitation.", "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
            resp["X-Request-ID"] = cid
            return resp

        # 3) Email d’invitation (ne doit JAMAIS faire tomber l’endpoint)
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
            sent_info = {"email_sent": True, "email_error": None}
        except Exception as e:
            # En préprod, configure EMAIL_BACKEND=console pour éviter même ces erreurs
            sent_info = {"email_sent": False, "email_error": str(e)}

        logger.info(
            "[invite %s] success user_id=%s phone=%s email_sent=%s",
            cid, getattr(user, "id", None), phone_normal, sent_info.get("email_sent")
        )

        resp = Response(
            {"detail": "Chauffeur invité avec succès.", **sent_info},
            status=status.HTTP_201_CREATED,
        )
        resp["X-Request-ID"] = cid
        return resp


class DriverStatusView(APIView):
    """
    GET /api/drivers/me/status/  (JWT requis côté chauffeur)
    PATCH /api/drivers/me/status/  (permet de maj must_reset_password / onboarding_completed / category)
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
    
    def patch(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Pas de profil chauffeur."}, status=status.HTTP_404_NOT_FOUND)
        
        ser = DriverStatusSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)

        if "onboarding_completed" in ser.validated_data:
            driver.onboarding_completed = bool(ser.validated_data["onboarding_completed"])
        if "category" in ser.validated_data:
            driver.category = ser.validated_data["category"]
        
        driver.save()
        return Response({"detail": "Statut mis à jour."}, status=status.HTTP_200_OK)


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
        new_password = (request.data.get("new_password") or "").strip()

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
        driver.save(update_fields=["license_file", "id_card_file", "onboarding_completed"])

        if new_password:
            request.user.set_password(new_password)
            request.user.save()

            if driver.must_reset_password:
                driver.must_reset_password = False
                driver.save(update_fields=["must_reset_password"])

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
        logger.info(
            f"[DEBUG] Resend invite → {driver.full_name} (phone={phone_normal or driver.phone}) temp_pwd={temp_password}"
        )

    return Response({"detail": "Invitation renvoyée.", **sent_info}, status=200)


class DriverLoginView(APIView):
    """
    POST /api/drivers/login/
    Body: { "phone": "...", "password": "..." }

    - "phone" peut être un téléphone (n’importe quel format) ou un email.
    Réponse : même format que le login mobile standard :

    {
      "access": "...",
      "refresh": "...",
      "user": {
        "id": ...,
        "phone_number": "...",
        "email": "...",
        "userType": "driver",
        "full_name": "..."
      },
      "driver_profile": { ... }  # si véhicule principal dispo
    }
    """
    permission_classes = []

    def post(self, request):
        raw_ident = (request.data.get("phone") or "").strip()
        password = (request.data.get("password") or "").strip()

        if not raw_ident or not password:
            return Response(
                {"detail": "Téléphone/email et mot de passe requis."},
                status=status.HTTP_400_BAD_REQUEST
            )

        User = get_user_model()

        # 1) On retrouve l’utilisateur par email ou téléphone
        user = None

        if "@" in raw_ident:
            # login via email
            try:
                user = User.objects.get(email=raw_ident)
            except User.DoesNotExist:
                return Response({"detail": "Identifiants invalides."}, status=status.HTTP_401_UNAUTHORIZED)
        else:
            # login via téléphone
            normalized = normalize_gabon_phone(raw_ident)
            try:
                user = User.objects.get(phone_number=normalized)
            except User.DoesNotExist:
                # Fallback : ancien identifiant type "login" sans @
                try:
                    user = User.objects.get(email=raw_ident)
                except User.DoesNotExist:
                    return Response({"detail": "Identifiants invalides."}, status=status.HTTP_401_UNAUTHORIZED)

        # 2) Vérification du mot de passe
        if not user.check_password(password):
            return Response({"detail": "Identifiants invalides."}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response({"detail": "Compte inactif."}, status=status.HTTP_403_FORBIDDEN)

        # 2bis) Vérifier le statut du chauffeur (blocage)
        try:
            driver = user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Aucun profil chauffeur associé."}, status=status.HTTP_403_FORBIDDEN)

        if driver.is_blocked:
            msg = "Compte chauffeur bloqué."
            if driver.block_reason:
                msg += f" Motif : {driver.block_reason}"
            return Response({"detail": msg}, status=status.HTTP_403_FORBIDDEN)

        # 3) On s’assure que le type est bien chauffeur/driver
        #    (build_auth_payload convertira "chauffeur" -> "driver")
        if getattr(user, "user_type", None) not in ("chauffeur", "driver"):
            # soit on force, soit on refuse. Ici on force le type chauffeur.
            user.user_type = "chauffeur"
            user.save(update_fields=["user_type"])

        # 4) Réponse standardisée (access, refresh, user{...}, driver_profile? )
        data = build_auth_payload(user)
        return Response(data, status=status.HTTP_200_OK)


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
    
class DriverChangePasswordView(APIView):
    """
    POST /api/drivers/me/change-password/
    Body: { "current_password": "...", "new_password": "..." }
    Effets:
      - vérifie l'ancien MDP
      - change le MDP
      - met must_reset_password=False
    JWT chauffeur requis
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        current = (request.data.get("current_password") or "").strip()
        new = (request.data.get("new_password") or "").strip()

        if not current or not new:
            return Response({"detail": "Champs requis."}, status=400)

        # Doit être chauffeur
        try:
            driver = user.driver_profile
        except Driver.DoesNotExist:
            return Response({"detail": "Aucun profil chauffeur associé."}, status=403)

        if not user.check_password(current):
            return Response({"detail": "Mot de passe actuel invalide."}, status=400)

        user.set_password(new)
        user.save()

        if driver.must_reset_password:
            driver.must_reset_password = False
            driver.save(update_fields=["must_reset_password"])

        return Response({"detail": "Mot de passe modifié."}, status=200)
    
class DriverBlockView(APIView):
    """
    PATCH /api/drivers/<pk>/block/
    Body: { "is_blocked": true/false, "block_reason": "..." (optionnel) }

    - is_blocked=true  -> on bloque + désactive le user
    - is_blocked=false -> on débloque + réactive le user
    """
    permission_classes = [IsAdminUser]

    def patch(self, request, pk):
        driver = get_object_or_404(Driver.objects.select_related("user"), pk=pk)
        is_blocked = bool(request.data.get("is_blocked", True))
        reason = (request.data.get("block_reason") or "").strip()

        driver.is_blocked = is_blocked
        if is_blocked:
            driver.block_reason = reason or "Compte chauffeur bloqué par l'administration."
        else:
            driver.block_reason = ""

        driver.save(update_fields=["is_blocked", "block_reason"])

        # on aligne l'utilisateur lié
        user = driver.user
        if user:
            user.is_active = not is_blocked
            user.save(update_fields=["is_active"])

        return Response(
            {
                "detail": "Chauffeur bloqué." if is_blocked else "Chauffeur débloqué.",
                "is_blocked": driver.is_blocked,
                "block_reason": driver.block_reason,
            },
            status=status.HTTP_200_OK,
        )

@api_view(["PATCH"])
@permission_classes([IsAdminUser])
def driver_block_toggle(request, pk: int):
    """
    PATCH /api/drivers/<pk>/block/
    Body: { "is_blocked": true|false, "block_reason"?: "..." }
    """
    driver = get_object_or_404(Driver.objects.select_related("user"), pk=pk)

    is_blocked = request.data.get("is_blocked", None)
    if is_blocked is None:
        return Response({"detail": "Le champ 'is_blocked' est requis."}, status=400)

    is_blocked = bool(is_blocked)
    reason = (request.data.get("block_reason") or "").strip()

    driver.is_blocked = is_blocked
    driver.block_reason = reason if is_blocked else ""
    driver.save(update_fields=["is_blocked", "block_reason"])

    return Response(
        {
            "detail": "Statut chauffeur mis à jour.",
            "is_blocked": driver.is_blocked,
            "block_reason": driver.block_reason,
        },
        status=200,
    )