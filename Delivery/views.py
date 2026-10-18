# Delivery/views.py
from django.db import transaction
from django.db.models import Q
from django.templatetags.static import static
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework import viewsets, permissions, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from .permissions import IsStaffOrAdmin
from django.core.mail import send_mail
from rest_framework.response import Response
import re

from .models import DeliveryRequest, DeliveryOffer, DeliveryDriver
from .serializers import (
    DeliveryRequestCreateSerializer,
    DeliveryRequestOutSerializer,
    DeliveryOfferOutSerializer,
    DeliveryDriverProfileSerializer,
    DeliveryDriverOnboardingSerializer,
    DeliveryDriverStatsSerializer,
    DeliveryDriverCreateSerializer,
    DeliveryDriverAdminListSerializer,
    DeliveryDriverAdminDetailSerializer,
    DeliveryDirectionsInSerializer,
    DeliveryDirectionsOutSerializer,
)

from .services.dispatch import create_offers_for_request, expire_offers_and_request_if_needed, publish_delivery_status
from django.contrib.auth import authenticate, get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
# ✅ ADDED: FCM helpers (ton fichier notifications/fcm.py)
from notifications.fcm import (
    notify_delivery_driver_new_order,
    notify_client_update,
)
from Delivery.serializers import DeliveryDriverFCMTokenSerializer
from Delivery.utils.phone import normalize_gabon_phone
import secrets
from .services.directions import get_directions


User = get_user_model()

class DeliveryDriverLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw_phone = request.data.get("phone")
        phone = normalize_gabon_phone(raw_phone)
        password = request.data.get("password")

        if not phone or not password:
            return Response({"detail": "phone et password requis."}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ adapte selon ton AUTHENTICATION backend (username/email/phone)
        user = User.objects.filter(phone_number=phone, is_active=True).first()
        if not user or not user.check_password(password):
            return Response({"detail": "Identifiants invalides."}, status=status.HTTP_401_UNAUTHORIZED)

        # ✅ vérifier qu’il a bien un profil livreur
        delivery_driver = getattr(user, "delivery_driver_profile", None)
        if not delivery_driver:
            return Response({"detail": "Compte non autorisé (pas livreur)."}, status=status.HTTP_403_FORBIDDEN)

        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        return Response({
            "access": access,
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "phone_number": getattr(user, "phone_number", None),
                "email": getattr(user, "email", None),
                "userType": "delivery_driver",
                "full_name": getattr(user, "full_name", None) or f"{getattr(user,'first_name','')} {getattr(user,'last_name','')}".strip(),
            },
            "delivery_driver_profile": {
                "onboarding_completed": delivery_driver.onboarding_completed,
                "is_verified": delivery_driver.is_verified,
                "is_active": delivery_driver.is_active,
                "is_available": delivery_driver.is_available,
            }
        }, status=status.HTTP_200_OK)


class DeliveryDriverMeStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        delivery_driver = getattr(request.user, "delivery_driver_profile", None)
        if not delivery_driver:
            return Response({"detail": "Not a delivery driver."}, status=status.HTTP_403_FORBIDDEN)

        # must_reset_password: à brancher sur ta logique réelle
        # (si tu as déjà un flag côté user, remplace ici)
        must_reset_password = bool(getattr(request.user, "must_reset_password", False))

        return Response({
            "must_reset_password": must_reset_password,
            "onboarding_completed": delivery_driver.onboarding_completed,
            "is_verified": delivery_driver.is_verified,
            "is_active": delivery_driver.is_active,
            "is_available": delivery_driver.is_available,
        })


# ============================================================
# PERMISSION
# ============================================================

class IsDeliveryDriver(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and hasattr(request.user, "delivery_driver_profile")
        )


# ============================================================
# LIVREUR — PROFIL, ONBOARDING, DASHBOARD, STATS
# ============================================================

class DeliveryDriverViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated, IsDeliveryDriver]

    def get_delivery_driver(self, request):
        return request.user.delivery_driver_profile

    # -----------------------------
    # PROFIL
    # -----------------------------
    @action(detail=False, methods=["get"])
    def me(self, request):
        delivery_driver = self.get_delivery_driver(request)
        serializer = DeliveryDriverProfileSerializer(delivery_driver)
        return Response(serializer.data)

    # -----------------------------
    # ONBOARDING
    # -----------------------------
    @action(detail=False, methods=["patch"])
    def onboarding(self, request):
        delivery_driver = self.get_delivery_driver(request)

        serializer = DeliveryDriverOnboardingSerializer(
            delivery_driver,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({"ok": True, "onboarding_completed": True})
    
    @action(detail=False, methods=["post"], url_path="change-password")
    def change_password(self, request):
        user = request.user

        new_password = request.data.get("new_password")
        old_password = request.data.get("old_password")

        if not new_password:
            return Response({"detail": "new_password requis"}, status=400)
        if old_password:
            if not user.check_password(old_password):
                return Response({"detail": "Ancien mot de passe incorrect"}, status=400)

        try:
            validate_password(new_password, user)
        except ValidationError as e:
            return Response({"detail": e.messages}, status=400)

        user.set_password(new_password)
        # flag reset
        if hasattr(user, "must_reset_password"):
            user.must_reset_password = False

        user.save()
        return Response({"ok": True})

    # -----------------------------
    # STATUT
    # -----------------------------
    @action(detail=False, methods=["get"])
    def status(self, request):
        delivery_driver = self.get_delivery_driver(request)
        return Response({
            "is_verified": delivery_driver.is_verified,
            "onboarding_completed": delivery_driver.onboarding_completed,
            "is_active": delivery_driver.is_active,
            "is_available": delivery_driver.is_available,
        })

    # -----------------------------
    # DASHBOARD
    # -----------------------------
    @action(detail=False, methods=["get"])
    def active_requests(self, request):
        delivery_driver = self.get_delivery_driver(request)

        qs = DeliveryRequest.objects.filter(assigned_driver=delivery_driver).filter(
            Q(status__in=[DeliveryRequest.STATUS_ASSIGNED, DeliveryRequest.STATUS_PICKED_UP])
            | Q(status=DeliveryRequest.STATUS_DELIVERED, is_paid=False)
            | Q(status=DeliveryRequest.STATUS_DELIVERED, order_details__purchase_mode='buy_for_me', order_details__funding_mode='driver_advance', purchase_actual_amount__gt=0, purchase_reimbursed_at__isnull=True)
        ).order_by('-created_at')

        serializer = DeliveryRequestOutSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def history(self, request):
        delivery_driver = self.get_delivery_driver(request)

        qs = DeliveryRequest.objects.filter(
            assigned_driver=delivery_driver,
            status__in=[
                DeliveryRequest.STATUS_DELIVERED,
                DeliveryRequest.STATUS_CANCELLED,
                DeliveryRequest.STATUS_FAILED,
            ],
        ).order_by("-created_at")

        serializer = DeliveryRequestOutSerializer(qs, many=True)
        return Response(serializer.data)

    # -----------------------------
    # STATS
    # -----------------------------
    @action(detail=False, methods=["get"])
    def stats(self, request):
        delivery_driver = self.get_delivery_driver(request)
        serializer = DeliveryDriverStatsSerializer(delivery_driver)
        return Response(serializer.data)

    # -----------------------------
    # CYCLE DE VIE LIVRAISON
    # -----------------------------
    @action(detail=True, methods=["post"], url_path="mark-picked-up")
    def mark_picked_up(self, request, pk=None):
        delivery_driver = self.get_delivery_driver(request)

        from .services.cash import check_pickup_payment
        with transaction.atomic():
            req = DeliveryRequest.objects.select_for_update().filter(pk=pk, assigned_driver=delivery_driver).first()
            if req is None:
                return Response({'detail': 'Livraison introuvable.'}, status=404)
            if req.status == DeliveryRequest.STATUS_PICKED_UP:
                return Response(DeliveryRequestOutSerializer(req).data)
            if req.status != DeliveryRequest.STATUS_ASSIGNED:
                return Response({'detail': 'Statut incompatible.'}, status=400)
            check_pickup_payment(req)
            req.status = DeliveryRequest.STATUS_PICKED_UP
            req.save(update_fields=['status', 'updated_at'])

        try:
            publish_delivery_status(req)
        except Exception :
            pass  # ne bloque pas l’opération principale

        return Response({"ok": True})

    @action(detail=True, methods=["post"], url_path="mark-delivered")
    def mark_delivered(self, request, pk=None):
        delivery_driver = self.get_delivery_driver(request)

        with transaction.atomic():
            try:
                req = DeliveryRequest.objects.select_for_update().get(
                    pk=pk,
                    assigned_driver=delivery_driver
                )
            except DeliveryRequest.DoesNotExist:
                return Response({"detail": "Livraison introuvable."}, status=404)

            if req.status == DeliveryRequest.STATUS_DELIVERED:
                return Response(DeliveryRequestOutSerializer(req).data)
            if req.status != DeliveryRequest.STATUS_PICKED_UP:
                return Response({"detail": "Statut incompatible."}, status=400)

            from .services.cash import confirm_change
            confirm_change(req, request.data.get('change_returned'))
            delivery_driver = DeliveryDriver.objects.select_for_update().get(pk=delivery_driver.pk)
            req.status = DeliveryRequest.STATUS_DELIVERED
            req.final_price = req.final_price or req.estimated_price
            req.save(update_fields=["status", "final_price", "purchase_change_returned_at", "updated_at"])

            # Stats livreur
            delivery_driver.total_deliveries += 1
            delivery_driver.is_available = True
            delivery_driver.save(update_fields=[
                "total_deliveries",
                "is_available",
                "updated_at"
            ])
        try:
            publish_delivery_status(req)
        except Exception :
            pass  # ne bloque pas l’opération principale

        return Response({"ok": True})


    @action(detail=True, methods=['post'], url_path='confirm-cash')
    def confirm_cash(self, request, pk=None):
        from .services.cash import fee, amount
        driver = self.get_delivery_driver(request)
        with transaction.atomic():
            req = DeliveryRequest.objects.select_for_update().filter(pk=pk, assigned_driver=driver).first()
            if req is None:
                return Response({'detail': 'Livraison introuvable.'}, status=404)
            if req.status not in ['assigned', 'picked_up', 'delivered'] or req.payment_method != 'cash':
                return Response({'detail': 'Encaissement indisponible pour cette livraison.'}, status=400)
            if req.is_paid:
                return Response(DeliveryRequestOutSerializer(req).data)
            if req.payment_payer != 'sender' and req.status == 'assigned':
                return Response({'detail': 'Le destinataire règle les frais à la remise, après le retrait.'}, status=400)
            expected = fee(req)
            if request.data.get('received') is not True or amount(request.data.get('amount')) != expected:
                return Response({'detail': 'Confirmez le montant exact des frais de livraison reçus en espèces.'}, status=400)
            driver = DeliveryDriver.objects.select_for_update().get(pk=driver.pk)
            req.is_paid = True
            req.cash_received_at = timezone.now()
            req.final_price = expected
            req.save(update_fields=['is_paid', 'cash_received_at', 'final_price', 'updated_at'])
            from .services.accounting import record_cash_allocation
            record_cash_allocation(req, driver, expected)
            publish_delivery_status(req, push=False)
        return Response(DeliveryRequestOutSerializer(req).data)

    @action(detail=True, methods=['post'], url_path='purchase-funds')
    def purchase_funds(self, request, pk=None):
        from .services.cash import purchases, amount
        with transaction.atomic():
            req = DeliveryRequest.objects.select_for_update().filter(pk=pk, assigned_driver=self.get_delivery_driver(request)).first()
            if req is None:
                return Response({'detail': 'Livraison introuvable.'}, status=404)
            details = purchases(req)
            if req.status != 'assigned' or not details or details.get('funding_mode', 'client_funded') != 'client_funded':
                return Response({'detail': 'Réception des fonds indisponible pour cette commande.'}, status=400)
            value = amount(request.data.get('amount'), 'Fonds reçus')
            if request.data.get('received') is not True or value > int(details.get('budget') or 0):
                return Response({'detail': 'Confirmez les fonds réellement reçus, dans la limite du budget.'}, status=400)
            if req.purchase_funds_received_at and value != req.purchase_funds_amount:
                return Response({'detail': 'Les fonds ont déjà été confirmés avec un autre montant.'}, status=409)
            req.purchase_funds_amount = value
            req.purchase_funds_received_at = req.purchase_funds_received_at or timezone.now()
            req.save(update_fields=['purchase_funds_amount', 'purchase_funds_received_at', 'updated_at'])
            publish_delivery_status(req, push=False)
        return Response(DeliveryRequestOutSerializer(req).data)

    @action(detail=True, methods=['post'], url_path='purchase-amount')
    def purchase_amount(self, request, pk=None):
        from .services.cash import record_purchase_amount
        with transaction.atomic():
            req = DeliveryRequest.objects.select_for_update().filter(pk=pk, assigned_driver=self.get_delivery_driver(request)).first()
            if req is None:
                return Response({'detail': 'Livraison introuvable.'}, status=404)
            if req.status != 'assigned' and not (req.status == 'picked_up' and req.purchase_actual_amount is None):
                return Response({'detail': 'Le montant des achats ne peut plus être modifié.'}, status=400)
            record_purchase_amount(req, request.data.get('amount'))
            publish_delivery_status(req, push=False)
        return Response(DeliveryRequestOutSerializer(req).data)

    @action(detail=True, methods=['post'], url_path='purchase-reimbursement')
    def purchase_reimbursement(self, request, pk=None):
        from .services.cash import purchases, amount
        with transaction.atomic():
            req = DeliveryRequest.objects.select_for_update().filter(pk=pk, assigned_driver=self.get_delivery_driver(request)).first()
            if req is None:
                return Response({'detail': 'Livraison introuvable.'}, status=404)
            details = purchases(req)
            if req.status not in ['assigned', 'picked_up', 'delivered'] or not details or details.get('funding_mode') != 'driver_advance' or not req.purchase_advance_accepted_at or not req.purchase_actual_amount:
                return Response({'detail': 'Remboursement indisponible pour cette commande.'}, status=400)
            if req.payment_payer != 'sender' and req.status == 'assigned':
                return Response({'detail': 'Le destinataire rembourse les achats à la remise.'}, status=400)
            if request.data.get('received') is not True or amount(request.data.get('amount')) != req.purchase_actual_amount:
                return Response({'detail': 'Confirmez le montant réel des achats remboursés ; les frais de livraison sont séparés.'}, status=400)
            req.purchase_reimbursed_at = req.purchase_reimbursed_at or timezone.now()
            req.save(update_fields=['purchase_reimbursed_at', 'updated_at'])
            publish_delivery_status(req, push=False)
        return Response(DeliveryRequestOutSerializer(req).data)


# ============================================================
# CLIENT — DELIVERY REQUEST
# ============================================================

class DeliveryRequestViewSet(mixins.CreateModelMixin,
                            mixins.ListModelMixin,
                            mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['require_quote'] = self.action == 'create'
        return context

    @action(detail=False, methods=['post'])
    def quote(self, request):
        from .services.quotes import issue_quote
        serializer = DeliveryRequestCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        return Response(issue_quote(serializer.validated_data, request.user))

    def get_queryset(self):
        return DeliveryRequest.objects.filter(user=self.request.user).order_by("-id")

    def get_serializer_class(self):
        if self.action == "create":
            return DeliveryRequestCreateSerializer
        return DeliveryRequestOutSerializer

    def perform_create(self, serializer):
        req = serializer.save()

        # VAN = opérateur humain
        if req.status == DeliveryRequest.STATUS_OPERATOR_REQUIRED:
            return

        # Dispatch auto
        created = create_offers_for_request(req)

        if created == 0:
            req.status = DeliveryRequest.STATUS_NO_DRIVER
            req.save(update_fields=["status", "updated_at"])
            try:
                publish_delivery_status(req)
            except Exception :
                pass  # ne bloque pas l’opération principale
            return

    @action(detail=True, methods=["get"])
    def refresh_status(self, request, pk=None):
        req = self.get_object()
        expire_offers_and_request_if_needed(req)
        req.refresh_from_db()
        return Response(DeliveryRequestOutSerializer(req).data)
    
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        req = self.get_object()

        if req.status not in [
            DeliveryRequest.STATUS_SEARCHING,
            DeliveryRequest.STATUS_OPERATOR_REQUIRED,
        ]:
            return Response(
                {"detail": "Impossible d’annuler cette demande."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        req.status = DeliveryRequest.STATUS_CANCELLED
        req.save(update_fields=["status", "updated_at"])

        try:
            publish_delivery_status(req)
        except Exception:
            pass

        return Response({"ok": True, "status": req.status})
    
    @action(detail=True, methods=["get"])
    def driver_location(self, request, pk=None):
        req = self.get_object()

        if req.status not in [DeliveryRequest.STATUS_ASSIGNED, DeliveryRequest.STATUS_PICKED_UP]:
            return Response({'detail': 'Le suivi de position est clôturé pour cette livraison.'}, status=400)

        if not req.assigned_driver:
            return Response({"detail": "Pas de livreur assigné"}, status=400)

        d = req.assigned_driver

        return Response({
            "lat": d.last_lat,
            "lng": d.last_lng,
            "updated_at": d.updated_at,
        })

# ============================================================
# LIVREUR — OFFERS
# ============================================================

class DeliveryOfferViewSet(mixins.ListModelMixin,
                           mixins.RetrieveModelMixin,
                           viewsets.GenericViewSet):
    permission_classes = [permissions.IsAuthenticated, IsDeliveryDriver]
    serializer_class = DeliveryOfferOutSerializer

    def get_queryset(self):
        delivery_driver: DeliveryDriver = self.request.user.delivery_driver_profile
        now = timezone.now()
        return DeliveryOffer.objects.filter(
            driver=delivery_driver,
            status=DeliveryOffer.STATUS_PENDING,
            expires_at__gt=now,
            request__status=DeliveryRequest.STATUS_SEARCHING,
        ).select_related("request").order_by("distance_km")

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        delivery_driver: DeliveryDriver = request.user.delivery_driver_profile
        now = timezone.now()

        offer = DeliveryOffer.objects.filter(pk=pk, driver=delivery_driver).first()
        if not offer:
            return Response({"detail": "Offer not found."}, status=status.HTTP_404_NOT_FOUND)

        if offer.status != DeliveryOffer.STATUS_PENDING:
            return Response({"detail": "Offer not pending."}, status=status.HTTP_400_BAD_REQUEST)

        if offer.expires_at <= now:
            offer.status = DeliveryOffer.STATUS_EXPIRED
            offer.responded_at = now
            offer.save(update_fields=["status", "responded_at"])
            expire_offers_and_request_if_needed(offer.request)
            return Response({"detail": "Offer expired."}, status=status.HTTP_400_BAD_REQUEST)

        offer.status = DeliveryOffer.STATUS_REJECTED
        offer.responded_at = now
        offer.save(update_fields=["status", "responded_at"])

        expire_offers_and_request_if_needed(offer.request)

        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        delivery_driver: DeliveryDriver = request.user.delivery_driver_profile
        now = timezone.now()

        with transaction.atomic():
            offer = (
                DeliveryOffer.objects
                .select_for_update()
                .select_related("request")
                .filter(pk=pk, driver=delivery_driver)
                .first()
            )

            if not offer:
                return Response({"detail": "Offer not found."}, status=status.HTTP_404_NOT_FOUND)

            req = (
                DeliveryRequest.objects
                .select_for_update()
                .filter(pk=offer.request_id)
                .first()
            )

            if offer.status != DeliveryOffer.STATUS_PENDING:
                return Response({"detail": "Offer not pending."}, status=status.HTTP_400_BAD_REQUEST)

            if offer.expires_at <= now:
                offer.status = DeliveryOffer.STATUS_EXPIRED
                offer.responded_at = now
                offer.save(update_fields=["status", "responded_at"])
                expire_offers_and_request_if_needed(req)
                return Response({"detail": "Offer expired."}, status=status.HTTP_400_BAD_REQUEST)

            if req.status != DeliveryRequest.STATUS_SEARCHING:
                return Response(
                    {"detail": f"Request not searchable ({req.status})."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if req.assigned_driver_id is not None:
                return Response({"detail": "Request already assigned."}, status=status.HTTP_409_CONFLICT)

            # Lock livreur
            delivery_driver = DeliveryDriver.objects.select_for_update().get(pk=delivery_driver.pk)

            if not delivery_driver.is_available or not delivery_driver.is_active:
                return Response({"detail": "Livreur indisponible."}, status=status.HTTP_400_BAD_REQUEST)

            if delivery_driver.is_blocked or not delivery_driver.is_verified or not delivery_driver.onboarding_completed:
                return Response({'detail': 'Compte livreur non autorisé à accepter une commande.'}, status=403)

            details = req.order_details or {}
            if details.get('purchase_mode') == 'buy_for_me' and details.get('funding_mode') == 'driver_advance':
                if request.data.get('accept_purchase_advance') is not True:
                    return Response({'detail': 'Acceptez explicitement l’avance des achats avant de prendre cette commande.'}, status=400)
                req.purchase_advance_accepted_at = now

            # Assignation
            req.assigned_driver = delivery_driver
            req.status = DeliveryRequest.STATUS_ASSIGNED
            req.assigned_by_operator = False
            req.save(update_fields=["assigned_driver", "status", "assigned_by_operator", "purchase_advance_accepted_at", "updated_at"])

            delivery_driver.is_available = False
            delivery_driver.save(update_fields=["is_available", "updated_at"])

            # Marquer offer acceptée
            offer.status = DeliveryOffer.STATUS_ACCEPTED
            offer.responded_at = now
            offer.save(update_fields=["status", "responded_at"])

            # Expirer les autres
            DeliveryOffer.objects.filter(
                request=req,
                status=DeliveryOffer.STATUS_PENDING,
            ).exclude(id=offer.id).update(
                status=DeliveryOffer.STATUS_EXPIRED,
                responded_at=now,
            )
        try:
            publish_delivery_status(req)
        except Exception :
            pass  # ne bloque pas l’opération principale

        return Response({"ok": True, "request_id": req.id})


# ============================================================
# LIVREUR — POSITION
# ============================================================

class DeliveryDriverLocationViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated, IsDeliveryDriver]

    @action(detail=False, methods=["post"], url_path="update-location")
    def update_location(self, request):
        delivery_driver = request.user.delivery_driver_profile
        lat = request.data.get("lat")
        lng = request.data.get("lng")

        if lat is None or lng is None:
            return Response({"detail": "lat/lng required"}, status=400)

        import math
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return Response({"detail": "Coordonnées invalides."}, status=400)
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            return Response({"detail": "Coordonnées invalides."}, status=400)
        delivery_driver.last_lat = lat
        delivery_driver.last_lng = lng
        delivery_driver.last_location_update = timezone.now()
        delivery_driver.save(update_fields=["last_lat", "last_lng", "last_location_update", "updated_at"])

        return Response({"ok": True})


# ============================================================
# OPÉRATEUR
# ============================================================

class OperatorDeliveryQueueViewSet(mixins.ListModelMixin,
                                  mixins.RetrieveModelMixin,
                                  viewsets.GenericViewSet):
    permission_classes = [permissions.IsAdminUser]
    serializer_class = DeliveryRequestOutSerializer

    def get_queryset(self):
        return DeliveryRequest.objects.filter(
            status__in=[
                DeliveryRequest.STATUS_OPERATOR_REQUIRED,
                DeliveryRequest.STATUS_FAILED,
            ]
        ).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def mark_handled(self, request, pk=None):
        obj = self.get_object()
        obj.operator_note = request.data.get("operator_note", obj.operator_note)
        obj.operator_handled_at = timezone.now()
        obj.save(update_fields=["operator_note", "operator_handled_at", "updated_at"])
        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def assign_driver(self, request, pk=None):
        obj = self.get_object()
        delivery_driver_id = request.data.get("driver_id")

        if not delivery_driver_id:
            return Response({"error": "driver_id requis"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            delivery_driver = DeliveryDriver.objects.get(id=delivery_driver_id, is_active=True)
        except DeliveryDriver.DoesNotExist:
            return Response({"error": "Livreur introuvable"}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            obj = DeliveryRequest.objects.select_for_update().get(pk=obj.pk)
            delivery_driver = DeliveryDriver.objects.select_for_update().get(pk=delivery_driver.pk)

            if not delivery_driver.is_available:
                return Response({"error": "Livreur indisponible"}, status=status.HTTP_409_CONFLICT)

            obj.assigned_driver = delivery_driver
            obj.status = DeliveryRequest.STATUS_ASSIGNED
            obj.assigned_by_operator = True
            obj.operator_handled_at = timezone.now()
            obj.save(update_fields=[
                "assigned_driver",
                "status",
                "assigned_by_operator",
                "operator_handled_at",
                "updated_at"
            ])

            delivery_driver.is_available = False
            delivery_driver.save(update_fields=["is_available", "updated_at"])
        try:
            notify_client_update(obj.user, "delivery", obj.id, obj.status)
        except Exception :
            pass  # ne bloque pas l’opération principale

        return Response({"ok": True, "assigned_driver": delivery_driver.id})
    
class DeliveryDriverFCMTokenView(APIView):
    permission_classes = [IsAuthenticated, IsDeliveryDriver]

    def post(self, request):
        delivery_driver = request.user.delivery_driver_profile

        serializer = DeliveryDriverFCMTokenSerializer(
            delivery_driver,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({"ok": True})

class DeliveryDriverCreateView(APIView):
    permission_classes = [AllowAny]  # à sécuriser plus tard

    def post(self, request):
        serializer = DeliveryDriverCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = serializer.save()

        self.send_invite_email(request, driver)

        return Response({
            "ok": True,
            "driver_id": driver.id,
            "user_id": driver.user.id,
            "phone": driver.phone,
            "email": driver.user.email,
        }, status=status.HTTP_201_CREATED)
    
    def send_invite_email(self, request, driver):
        user = driver.user

        temp_password = secrets.token_urlsafe(8)
        user.set_password(temp_password)
        user.must_reset_password = True
        user.save()

        logo_url = request.build_absolute_uri(static("img/blaze-logo.png"))

        subject = "Votre accès Blaze – Livreur"

        html_message = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>Accès Blaze</title>
    </head>
    <body style="margin:0;padding:0;background:#f2f4f7;font-family:Arial,Helvetica,sans-serif;">
        <div style="max-width:600px;margin:30px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.08);">
    
            <div style="background:#052D4F;color:#ffffff;padding:20px;text-align:center;">
                <img src="{logo_url}" alt="Blaze" style="max-width:160px;margin-bottom:10px;" />
                <h1 style="margin:0;font-size:22px;">Blaze – Accès Livreur</h1>
            </div>

            <div style="padding:24px;color:#333;">
            <p>Bonjour <strong>{driver.full_name}</strong>,</p>

            <p>Votre compte livreur Blaze a été activé. Voici vos identifiants :</p>

            <div style="background:#f5f7fa;border-radius:8px;padding:16px;margin:16px 0;">
                <p style="margin:4px 0;"><strong>Téléphone :</strong> {driver.phone}</p>
                <p style="margin:4px 0;"><strong>Mot de passe temporaire :</strong> {temp_password}</p>
            </div>

            <p>Merci de vous connecter et de changer votre mot de passe immédiatement.</p>

            <div style="text-align:center;margin:24px 0;">
                <a href="https://be.blazepro.org"
                    style="background:#29B44E;color:#000;text-decoration:none;padding:14px 22px;border-radius:8px;font-weight:bold;display:inline-block;">
                Se connecter à Blaze
                </a>
            </div>

            <p style="font-size:13px;color:#666;">
                Si vous n’êtes pas à l’origine de cette demande, veuillez ignorer ce message.
            </p>
        </div>

        <div style="background:#f2f4f7;padding:14px;text-align:center;font-size:12px;color:#888;">
            © {timezone.now().year} Blaze • Tous droits réservés
        </div>

    </div>
</body>
</html>
"""

        text_message = f"""
Bonjour {driver.full_name},

Voici vos accès Blaze :

Téléphone : {driver.phone}
Mot de passe temporaire : {temp_password}

Merci de vous connecter et de changer votre mot de passe immédiatement.

Lien : https://be.blazepro.org
Si vous n’êtes pas à l’origine de cette demande, veuillez ignorer ce message.

"""

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=None,
            to=[user.email],
        )
        msg.attach_alternative(html_message, "text/html")
        msg.send()

class DeliveryDriverAdminViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsStaffOrAdmin]

    def list(self, request):
        qs = DeliveryDriver.objects.select_related("user").order_by("-created_at")
        serializer = DeliveryDriverAdminListSerializer(qs, many=True)
        return Response(serializer.data)

    def retrieve(self, request, pk=None):
        driver = DeliveryDriver.objects.select_related("user").get(pk=pk)
        serializer = DeliveryDriverAdminDetailSerializer(driver)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def resend_invite(self, request, pk=None):
        driver = DeliveryDriver.objects.select_related("user").get(pk=pk)
        user = driver.user

        if not user.email:
            return Response(
                {"detail": "Ce livreur n'a pas d'email valide."},
                status=400
            )

        temp_password = secrets.token_urlsafe(8)
        user.set_password(temp_password)
        user.must_reset_password = True
        user.save()

        subject = "Votre accès Blaze – Livreur"

        logo_url = request.build_absolute_uri(
            static("img/blaze-logo.png")
        )

        html_message = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>Accès Blaze</title>
    </head>
    <body style="margin:0;padding:0;background:#f2f4f7;font-family:Arial,Helvetica,sans-serif;">
        <div style="max-width:600px;margin:30px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.08);">
    
            <div style="background:#052D4F;color:#ffffff;padding:20px;text-align:center;">
                <img src="{logo_url}" alt="Blaze" style="max-width:160px;margin-bottom:10px;" />
                <h1 style="margin:0;font-size:22px;">Blaze – Accès Livreur</h1>
            </div>

            <div style="padding:24px;color:#333;">
            <p>Bonjour <strong>{driver.full_name}</strong>,</p>

            <p>Votre compte livreur Blaze a été activé. Voici vos identifiants :</p>

            <div style="background:#f5f7fa;border-radius:8px;padding:16px;margin:16px 0;">
                <p style="margin:4px 0;"><strong>Téléphone :</strong> {driver.phone}</p>
                <p style="margin:4px 0;"><strong>Mot de passe temporaire :</strong> {temp_password}</p>
            </div>

            <p>Merci de vous connecter et de changer votre mot de passe immédiatement.</p>

            <div style="text-align:center;margin:24px 0;">
                <a href="https://be.blazepro.org"
                    style="background:#29B44E;color:#000;text-decoration:none;padding:14px 22px;border-radius:8px;font-weight:bold;display:inline-block;">
                Se connecter à Blaze
                </a>
            </div>

            <p style="font-size:13px;color:#666;">
                Si vous n’êtes pas à l’origine de cette demande, veuillez ignorer ce message.
            </p>
        </div>

        <div style="background:#f2f4f7;padding:14px;text-align:center;font-size:12px;color:#888;">
            © {timezone.now().year} Blaze • Tous droits réservés
        </div>

    </div>
</body>
</html>
"""
        text_message = f"""
Bonjour {driver.full_name},

Voici vos accès Blaze :

Téléphone : {driver.phone}
Mot de passe temporaire : {temp_password}

Merci de vous connecter et de changer votre mot de passe immédiatement.

Lien : https://be.blazepro.org
Si vous n’êtes pas à l’origine de cette demande, veuillez ignorer ce message.
"""
        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_message,
                from_email=None,
                to=[user.email],
            )
            msg.attach_alternative(html_message, "text/html")
            msg.send()
        except Exception as e:
            return Response(
                {"detail": f"Erreur lors de l'envoi du mail: {str(e)}"},
                status=500
            )

        return Response({"detail": "Invitation renvoyée."})

    @action(detail=True, methods=["post"])
    def toggle_block(self, request, pk=None):
        driver = DeliveryDriver.objects.get(pk=pk)
        is_blocked = request.data.get("is_blocked")
        reason = request.data.get("block_reason", "")

        driver.is_blocked = is_blocked
        driver.block_reason = reason if is_blocked else ""
        driver.is_active = False if is_blocked else driver.is_active
        driver.save()

        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def validate_driver(self, request, pk=None):
        driver = DeliveryDriver.objects.get(pk=pk)
        driver.is_verified = True
        driver.is_active = True
        driver.save()

        return Response({"ok": True})
    
class DeliveryDirectionsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = DeliveryDirectionsInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            result = get_directions(**ser.validated_data)
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        out = DeliveryDirectionsOutSerializer(result)
        return Response(out.data, status=status.HTTP_200_OK)