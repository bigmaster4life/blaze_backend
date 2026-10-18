# RideVTC/views.py

from rest_framework.viewsets import ModelViewSet
from rest_framework.views import APIView
from .models import RideVehicle, Ride, Payment, DriverStats, DriverRating, DriverPresence, DriverNavEvent
from .serializers import (
    RideVehicleSerializer,
    RideCreateSerializer,
    RideSerializer,
    RideOutSerializer,
    DriverLocationSerializer,
    RateDriverSerializer,
    DriverVehicleMeSerializer,
    DriverNavEventSerializer,
    OperatorRideSerializer,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status, permissions, filters, mixins
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from .utils.rooms import user_room, driver_room, pool_room
from RideVTC.utils.payments import (
    normalize_msisdn,
    select_provider,
    provider_init_payment,
    verify_and_parse,
    map_status,
)
from django.utils import timezone
from django.conf import settings
from RideVTC.utils.payloads import build_ride_offer_payload
import re
import logging
import time
from decimal import Decimal
from math import ceil
from django.db.models import Count, Avg
from django.db.models.functions import TruncHour
from .permissions import IsDriverOrStaff
from users.models import CustomerProfile
from .dispatch import dispatch_ride_offer


from asgiref.sync import async_to_sync
try:
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
except Exception:
    channel_layer = None

from .ws import app_ws_send  # mock WS; remplace par ta vraie intégration si dispo

logger = logging.getLogger(__name__)

def _has_success_payment(ride):
    return Payment.objects.filter(ride=ride, status="SUCCESS").exists()

def ride_has_success_payment(ride: Ride) -> bool:
    return Payment.objects.filter(ride=ride, status='SUCCESS').exists()

def _pause_seconds_now(ride):
    total = ride.total_pause_seconds or 0
    if ride.pause_started_at:
        total += int((timezone.now() - ride.pause_started_at).total_seconds())
    return total

def _compute_pause_fee(total_seconds: int) -> int:
    free = int(getattr(settings, "PAUSE_FREE_SECONDS", 300))
    rate = int(getattr(settings, "PAUSE_RATE_PER_MIN", 250))
    extra = max(0, total_seconds - free)
    mins = ceil(extra / 60) if extra > 0 else 0
    return mins * rate  # XAF (int)



class IsDriver(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        raw_type = (getattr(user, "user_type", "") or "").lower()
        return raw_type in ("chauffeur", "driver")


class RideVehicleViewSet(ModelViewSet):
    serializer_class = RideVehicleSerializer
    permission_classes = [AllowAny]

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [permissions.IsAuthenticated(), IsDriver()]

    def perform_create(self, serializer):
        serializer.save(driver=self.request.user)

    @transaction.atomic
    def perform_update(self, serializer):
        from .models import SharedTrip
        from django.contrib.auth import get_user_model
        from rest_framework.exceptions import ValidationError
        get_user_model().objects.select_for_update().get(pk=self.request.user.pk)
        if SharedTrip.objects.filter(vehicle=serializer.instance, state='active').exists():
            raise ValidationError('Terminez le trajet partagé avant de modifier le véhicule.')
        serializer.save(driver=self.request.user)

    @transaction.atomic
    def perform_destroy(self, instance):
        from django.contrib.auth import get_user_model
        get_user_model().objects.select_for_update().get(pk=self.request.user.pk)
        from .models import SharedTrip
        from rest_framework.exceptions import ValidationError
        if SharedTrip.objects.filter(vehicle=instance).exists():
            raise ValidationError('Ce véhicule est lié à un trajet partagé.')
        instance.delete()

    def get_queryset(self):
        queryset = RideVehicle.objects.filter(available=True)
        if self.action not in ('list', 'retrieve'):
            queryset = queryset.filter(driver=self.request.user)
        city = self.request.query_params.get('city')
        category = self.request.query_params.get('category')

        if city:
            queryset = queryset.filter(city__iexact=city)
        if category and category.lower() != 'all':
            queryset = queryset.filter(category__iexact=category)

        return queryset


class RideViewSet(viewsets.ModelViewSet):
    """
    Endpoints:
      - GET    /api/rides/               (list)
      - GET    /api/rides/<id>/          (retrieve)
      - POST   /api/rides/               (create standard)
      - POST   /api/rides/create/        (alias rétro-compat)
      - GET    /api/rides/<id>/live/     (payload léger, sécurisé)
      - POST   /api/rides/<id>/accept/
      - POST   /api/rides/<id>/cancel/
      - POST   /api/rides/<id>/arrived/
      - POST   /api/rides/<id>/start/
      - POST   /api/rides/<id>/finish/
      - POST   /api/rides/<id>/location/        (driver -> client)
      - POST   /api/rides/<id>/rider-location/  (client -> driver)
    """
    queryset = Ride.objects.all().order_by("-id")
    permission_classes = [permissions.IsAuthenticated]

    def _pause_seconds_now(ride):
        """Retourne le total cumulé + la tranche en cours (si pause active)."""
        total = ride.total_pause_seconds or 0
        if ride.pause_started_at:
            total += int((timezone.now() - ride.pause_started_at).total_seconds())
        return total
    
    def _compute_pause_fee(total_seconds: int) -> int:
        free = getattr(settings, "PAUSE_FREE_SECONDS", 300)
        rate = int(getattr(settings, "PAUSE_RATE_PER_MIN", 250))
        extra = max(0, total_seconds - free)
        mins = ceil(extra / 60) if extra > 0 else 0
        return mins * rate  # XAF int

    def get_serializer_class(self):
        return RideCreateSerializer if self.action in ("create", "create_alias") else RideSerializer

    def update(self, request, *args, **kwargs):
        if get_object_or_404(Ride, pk=kwargs.get('pk')).ride_mode == 'shared':
            return Response({'detail': 'Utilisez les actions du trajet partagé.'}, status=405)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if get_object_or_404(Ride, pk=kwargs.get('pk')).ride_mode == 'shared':
            return Response({'detail': 'Utilisez l’annulation de la réservation.'}, status=405)
        return super().destroy(request, *args, **kwargs)

    # ───────────────────────────────────────────────────────────
    # LIVE: état minimal de la course (auth contrôlée)
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["get"], url_path="live")
    def live(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)

        # Autorisations: client ou chauffeur assigné ou staff
        user = request.user
        is_customer = getattr(ride, "user_id", None) == getattr(user, "id", None)
        is_driver   = getattr(ride, "driver_id", None) == getattr(user, "id", None)
        if not (is_customer or is_driver or user.is_staff):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        data = {
            "id": ride.id,
            "status": getattr(ride, "status", "pending"),
            "category": getattr(ride, "category", "") or "",
            "price": str(getattr(ride, "price", "") or ""),
            "pickup": {
                "label": getattr(ride, "pickup_location", "") or "",
                "lat": getattr(ride, "pickup_lat", None),
                "lng": getattr(ride, "pickup_lng", None),
            },
            "dropoff": {
                "label": getattr(ride, "dropoff_location", "") or "",
                "lat": getattr(ride, "dropoff_lat", None),
                "lng": getattr(ride, "dropoff_lng", None),
            },
            "driver": None,
            "driver_lat": getattr(ride, "driver_lat", None),
            "driver_lng": getattr(ride, "driver_lng", None),
        }
        if ride.driver_id:
            d = ride.driver
            vehicle = RideVehicle.objects.filter(driver=d).order_by("id").first()
            brand = getattr(vehicle, "brand", None) if vehicle else None
            model = getattr(vehicle, "model", None) if vehicle else None
            plate = None
            if vehicle:
                plate = (
                    getattr(vehicle, "plate", None)
                    or getattr(vehicle, "vehicle_plate", None)
                )
            color = getattr(vehicle, "color", None) if vehicle else None
            category = getattr(vehicle, "category", None) if vehicle else None
            stats = DriverStats.objects.filter(driver_id=ride.driver_id).first()
            rating_avg = getattr(stats, "rating_avg", None) if stats else None
            rides_done = getattr(stats, "rating_count", None) if stats else None

            data["driver"] = {
                "id": ride.driver_id,
                "email": getattr(d, "email", None),
                "first_name": getattr(d, "first_name", None),
                "last_name": getattr(d, "last_name", None),
                "phone": getattr(d, "phone_number", None),

                "brand": brand,
                "model": model,
                "plate": plate,
                "color": color,
                "category": category,
                "rating_avg": rating_avg,
                "rides_done": rides_done,
            }
        
        data["pause"] = {
            "active": bool(ride.pause_started_at),
            "started_at": ride.pause_started_at.isoformat() if ride.pause_started_at else None,
            "total_pause_s": _pause_seconds_now(ride),
            "free_seconds": getattr(settings, "PAUSE_FREE_SECONDS", 300),
            "rate_per_min": int(getattr(settings, "PAUSE_RATE_PER_MIN", 250)),
            "fee_so_far": _compute_pause_fee(_pause_seconds_now(ride)),
        }
        data["final_price"] = str(getattr(ride, "final_price", "") or "")
        data["pause_fee"] = int(getattr(ride, "pause_fee", 0) or 0)
        data["ride_mode"] = ride.ride_mode
        data["passenger_count"] = ride.passenger_count
        if hasattr(ride, "shared_booking"):
            booking = ride.shared_booking
            data["shared_trip_id"] = booking.trip_id
            data["shared_discount_percent"] = float(booking.discount_rate * 100)
            if ride.driver_id == request.user.pk:
                from .shared import trip_data
                data["shared_trip"] = trip_data(booking.trip)
        return Response(data, status=status.HTTP_200_OK)

    # ───────────────────────────────────────────────────────────
    # DRIVER → position → client
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="location")
    def location(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.ride_mode == 'shared':
            return Response({'detail': 'Utilisez le trajet partagé sans supplément.'}, status=409)
        if ride.driver_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        serializer = DriverLocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lat = serializer.validated_data['lat']
        lng = serializer.validated_data['lng']

        # Ne pousse pas si course terminée/annulée
        if ride.status in {"cancelled", "completed", "finished"}:
            logger.info("[LOC] reject location ride_id=%s status=%s", ride.id, ride.status)
            resp = Response(
                {"ok": False, "reason": "ride_ended", "ride_status": ride.status, "ride_id": ride.id},
                status=status.HTTP_410_GONE,
            )
            resp["X-Ride-Ended"] = "1"
            return resp

        ride.driver_lat = lat
        ride.driver_lng = lng
        ride.save(update_fields=['driver_lat', 'driver_lng'])
        from .supervision_presence import record_position
        record_position(request.user.pk, lat, lng)

        if channel_layer:
            payload = {
                "type": "ride.driver.location",
                "requestId": ride.id,
                "lat": lat,
                "lng": lng,
                "leg": "to_pickup" if ride.status == "accepted" else "to_dropoff"
            }
            async_to_sync(channel_layer.group_send)(
                user_room(ride.user_id),
                {"type": "evt", "event": "ride.driver.location", "payload": payload},
            )
        return Response({"ok": True})

    # ───────────────────────────────────────────────────────────
    # RIDER → position → driver (si tu veux la partager)
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="rider-location")
    def rider_location(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.user_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        serializer = DriverLocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lat = serializer.validated_data['lat']
        lng = serializer.validated_data['lng']

        # Exemple: on met à jour le champ pickup_* juste pour démo
        if ride.ride_mode != 'shared':
            ride.pickup_lat = lat
            ride.pickup_lng = lng
            ride.save(update_fields=['pickup_lat', 'pickup_lng'])

        if channel_layer and ride.driver_id:
            payload = {"type": "ride.rider.location", "requestId": ride.id, "lat": lat, "lng": lng}
            async_to_sync(channel_layer.group_send)(
                driver_room(ride.driver_id),
                {"type": "evt", "event": "ride.rider.location", "payload": payload},
            )
        return Response({"ok": True})

    # ───────────────────────────────────────────────────────────
    # CREATE → push ride.requested aux chauffeurs (pool.*)
    # ───────────────────────────────────────────────────────────
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        if ser.validated_data.get('ride_mode') == 'shared':
            from django.contrib.auth import get_user_model
            from django.db.models import Q
            from datetime import timedelta
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            active = Ride.objects.filter(user=request.user).filter(
                Q(status__in=['accepted', 'in_progress']) |
                Q(status='pending', requested_at__gte=timezone.now()-timedelta(seconds=60))
            ).order_by('-pk').first()
            if active:
                fields = ('ride_mode', 'passenger_count', 'category', 'pickup_lat', 'pickup_lng', 'dropoff_lat', 'dropoff_lng')
                if all(getattr(active, field) == ser.validated_data.get(field) for field in fields):
                    return Response(RideSerializer(active).data, status=200)
                return Response({'detail': 'Une réservation est déjà active.', 'active_ride_id': active.pk}, status=409)

        ride: Ride = ser.save(status="pending", accepted_at=None, completed_at=None)

        def _clean(s: str) -> str:
            import re
            return re.sub(r"[^0-9A-Za-z_.-]", "_", (s or "").strip().lower())[:50]

        raw_area = request.data.get("area") or "city-default"
        area = _clean(raw_area)

        # Catégorie demandée pour la course (par défaut 'eco')
        preferred_category = (
            request.data.get("category")
            or getattr(ride, "category", None)
            or "eco"
        )

        from .shared import try_join
        matched = try_join(ride) if ride.ride_mode == "shared" else False
        if matched:
            ride.refresh_from_db()
        # Dispatch après commit de la transaction
        if channel_layer and not matched:
            if ride:
                transaction.on_commit(
                    lambda: dispatch_ride_offer(
                        ride_id=ride.id,
                        area=area,
                        preferred_category=preferred_category,
                    )
                )

        return Response(RideSerializer(ride).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="create")
    def create_alias(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)

    # ───────────────────────────────────────────────────────────
    # ACCEPT → MAJ DB + push ride.accepted au client
    # ───────────────────────────────────────────────────────────
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="accept")
    def accept(self, request, pk=None):
        candidate = get_object_or_404(Ride, pk=pk)
        if candidate.ride_mode == "shared":
            from .shared import accept_first
            from rest_framework.exceptions import ValidationError
            try:
                trip = accept_first(candidate.pk, request.user)
            except ValidationError as exc:
                return Response({'detail': exc.detail}, status=409)
            return Response({"ok": True, "shared_trip_id": trip.pk})
        from .models import SharedTrip
        from django.contrib.auth import get_user_model
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if SharedTrip.objects.filter(driver=request.user, state="active").exists():
            return Response({"detail": "Terminez le trajet partagé actif."}, status=409)
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)

        # Si la course n'est plus "pending", on garde ton comportement actuel
        if ride.status != "pending":
            return Response({"detail": f"Already {ride.status}"}, status=409)
        
        if ride.driver_id and ride.driver_id != request.user.id:
            return Response(
                {"detail": "Ride reserved for another driver."},
                status=status.HTTP_409_CONFLICT,
            )

        # ───── Timeout d'acceptation (8–10s) ─────
        timeout_s = int(getattr(settings, "RIDE_ACCEPT_TIMEOUT_S", 10))
        created_at = getattr(ride, "created_at", None)

        if created_at is not None:
            age = (timezone.now() - created_at).total_seconds()
            if age > timeout_s:
                # l'offre est expirée pour ce driver
                return Response(
                    {
                        "detail": "Offer expired (timeout).",
                        "code": "offer_timeout",
                        "timeout_s": timeout_s,
                        "age_s": int(age),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        # Chauffeur authentifié → on l'assigne
        ride.driver = request.user
        ride.status = "accepted"
        if hasattr(ride, "accepted_at"):
            ride.accepted_at = timezone.now()
            ride.save(update_fields=["driver", "status", "accepted_at"])
        else:
            ride.save(update_fields=["driver", "status"])

        # WS → informer le client
        if channel_layer:
            try:
                client_group = user_room(ride.user_id)
                alt_group    = f"user.{ride.user_id}"
                payload = {
                    "requestId": ride.id,
                    "driver": {
                        "id": ride.driver_id,
                        "email": getattr(ride.driver, "email", None),
                        "phone_number": getattr(ride.driver, "phone_number", None),
                    },
                    "ride": {
                        "id": ride.id,
                        "pickup": {
                            "label": ride.pickup_location,
                            "lat": getattr(ride, "pickup_lat", None),
                            "lng": getattr(ride, "pickup_lng", None),
                        },
                        "dropoff": {
                            "label": ride.dropoff_location,
                            "lat": getattr(ride, "dropoff_lat", None),
                            "lng": getattr(ride, "dropoff_lng", None),
                        },
                        "price": str(ride.price),
                        "status": ride.status,
                        "pickup_label": (ride.pickup_location or None),
                        "dropoff_label": (ride.dropoff_location or None),
                        "price_text": f"{int(ride.price)} FCFA" if ride.price is not None else "—",
                    },
                }
                # Format générique
                evt_msg = {"type": "evt", "event": "ride.accepted", "payload": payload}
                async_to_sync(channel_layer.group_send)(client_group, evt_msg)
                async_to_sync(channel_layer.group_send)(alt_group,    evt_msg)

                # Format direct
                direct_msg = {
                    "type": "ride.accepted",
                    "requestId": ride.id,
                    "driver": payload["driver"],
                    "ride": payload["ride"],
                }
                async_to_sync(channel_layer.group_send)(client_group, direct_msg)
                async_to_sync(channel_layer.group_send)(alt_group,    direct_msg)

                # Notifier aussi le chauffeur
                async_to_sync(channel_layer.group_send)(
                    driver_room(ride.driver_id),
                    {"type": "evt", "event": "ride.assigned", "payload": {"requestId": ride.id}},
                )
                logger.info("[WS] accept: notified %s & %s ride_id=%s", client_group, alt_group, ride.id)
            except Exception:
                logger.exception("WS emit (accept) failed")

        return Response({"ok": True})
    
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="queue")
    def queue(self, request, pk=None):
        """
        Met la course en "attente" pour ce chauffeur :
          - status reste 'pending'
          - driver est fixé à request.user
          - les autres chauffeurs ne peuvent plus accepter (voir accept()).
        """
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)

        if ride.status != "pending":
            return Response(
                {"detail": f"Cannot queue ride in status '{ride.status}'"},
                status=status.HTTP_409_CONFLICT,
            )

        # Si déjà réservée pour un autre chauffeur
        if ride.driver_id and ride.driver_id != request.user.id:
            return Response(
                {"detail": "Ride already queued for another driver."},
                status=status.HTTP_409_CONFLICT,
            )

        # Réservation pour ce chauffeur (sans changer le status)
        ride.driver = request.user
        ride.save(update_fields=["driver"])

        # Optionnel : notifier le client qu'un chauffeur a réservé la course
        if channel_layer:
            try:
                payload = {
                    "requestId": ride.id,
                    "driver": {
                        "id": ride.driver_id,
                        "phone_number": getattr(ride.driver, "phone_number", None),
                    },
                }
                for g in {user_room(ride.user_id), f"user.{ride.user_id}"}:
                    async_to_sync(channel_layer.group_send)(
                        g,
                        {
                            "type": "evt",
                            "event": "ride.queued",
                            "payload": payload,
                        },
                    )
            except Exception:
                logger.exception("WS emit (queue) failed")

        return Response({"ok": True, "status": "pending", "driver_id": ride.driver_id})
    
    # ───────────────────────────────────────────────────────────
    # PAUSE: Client en pause + calcul du tarif
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="pause/start")
    def pause_start(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.ride_mode == 'shared':
            return Response({'detail': 'Les pauses ne sont pas facturées en partagé.'}, status=409)
        if not (request.user.is_staff or ride.driver_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=403)
        if ride.status not in {"accepted", "in_progress"}:
            return Response({"detail": f"Cannot pause in status {ride.status}"}, status=409)
        
        if ride.pause_started_at:
            total = _pause_seconds_now(ride)
            fee = _compute_pause_fee(total)
            return Response({"ok": True, "pause_active": True,
                             "total_pause_s": total, "pause_fee": fee}, status=200)
        ride.pause_started_at = timezone.now()
        ride.save(update_fields=["pause_started_at"])

        if channel_layer:
            payload = {
                "requestId": ride.id,
                "at": ride.pause_started_at.isoformat(),
                "freeRemaining": max(0, getattr(settings,"PAUSE_FREE_SECONDS",300) - (ride.total_pause_seconds or 0)),
            }
            for g in {user_room(ride.user_id), f"user.{ride.user_id}", driver_room(ride.driver_id)}:
                async_to_sync(channel_layer.group_send)(g, {"type":"evt","event":"ride.pause.started","payload":payload})
            
        return Response({"ok": True, "pause_active": True, "total_pause_s": ride.total_pause_seconds, "pause_fee": int(ride.pause_fee)}, status=200)
    
    @action(detail=True, methods=["post"], url_path="pause/stop")
    def pause_stop(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.ride_mode == 'shared':
            return Response({'detail': 'Les pauses ne sont pas facturées en partagé.'}, status=409)
        if not (request.user.is_staff or ride.driver_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=403)
        
        if not ride.pause_started_at:
            total = _pause_seconds_now(ride)
            fee = _compute_pause_fee(total)
            return Response({"ok": True, "pause_active": False, "total_pause_s": total, "pause_fee": fee}, status=200)
        
        # accumuler la tranche courante
        now = timezone.now()
        delta = int((now - ride.pause_started_at).total_seconds())
        ride.total_pause_seconds = (ride.total_pause_seconds or 0) + max(0, delta)
        ride.pause_started_at = None

        # recalculer le tarif de pause
        fee_int = _compute_pause_fee(ride.total_pause_seconds)
        ride.pause_fee = Decimal(fee_int)

        base = Decimal(ride.price or 0)
        ride.final_price = base + ride.pause_fee

        ride.save(update_fields=["total_pause_seconds", "pause_started_at", "pause_fee", "final_price"])

        if channel_layer:
            payload = {
                "requestId": ride.id,
                "total_pause_s": ride.total_pause_seconds,
                "pause_fee": int(ride.pause_fee),
                "final_price": str(ride.final_price or base),
            }
            for g in {user_room(ride.user_id), f"user.{ride.user_id}", driver_room(ride.driver_id)}:
                async_to_sync(channel_layer.group_send)(g, {"type":"evt","event":"ride.pause.stopped","payload":payload})
                async_to_sync(channel_layer.group_send)(g, {"type":"evt","event":"ride.fare.updated","payload":payload})

        return Response({"ok": True, "pause_active": False,
                         "total_pause_s": ride.total_pause_seconds,
                         "pause_fee": int(ride.pause_fee),
                         "final_price": str(ride.final_price)}, status=200)

    # ───────────────────────────────────────────────────────────
    # CANCEL → status + push à client & chauffeur
    # ───────────────────────────────────────────────────────────
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        from analytics.cancellations import parse_reason, record_cancellation
        from .shared import cancel_booking
        initial = get_object_or_404(Ride, pk=pk)
        if request.user.pk not in (initial.user_id, initial.driver_id) and not request.user.is_staff:
            return Response({"detail": "Forbidden"}, status=403)
        if initial.status in {"cancelled", "completed", "finished"}:
            return Response({"id": initial.pk, "status": initial.status})
        if initial.ride_mode == 'shared':
            shared_ride = cancel_booking(pk, request.user, reason_data=request.data)
            return Response({"id": shared_ride.pk, "status": shared_ride.status})
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)

        is_customer = (ride.user_id == request.user.id)
        is_driver = (ride.driver_id == request.user.id)
        if not (is_customer or is_driver or request.user.is_staff):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        if ride.status in {"cancelled", "completed", "finished"}:
            return Response({"id": ride.id, "status": ride.status}, status=200)

        if ride.status == 'in_progress':
            return Response({'detail': 'La course a démarré. Contactez l’assistance pour interrompre la prestation.'}, status=409)
        if ride.payments.filter(status='SUCCESS').exists():
            return Response({'detail': 'Un paiement est enregistré. Contactez l’assistance pour régler l’annulation.'}, status=409)
        before = ride.status
        reason = parse_reason(request.data, customer=is_customer, status_before=before)
        ride.status = "cancelled"
        ride.dispatch_status = 'cancelled'
        ride.save(update_fields=['dispatch_status'])
        record_cancellation(ride, request.user, before, reason)
        if hasattr(ride, "cancelled_at"):
            ride.cancelled_at = timezone.now()
            ride.save(update_fields=["status", "cancelled_at"])
        else:
            ride.save(update_fields=["status"])

        if channel_layer:
            try:
                payload = {"requestId": ride.id}
                # Client (toujours)
                client_group = user_room(ride.user_id)

                driver_groups = set()
                if ride.driver_id:
                    driver_groups.add(driver_room(ride.driver_id))

                    d = getattr(ride, "driver", None)
                    driver_ws_id = getattr(d, "user_id", None)
                    if driver_ws_id:
                        driver_groups.add(f"user.{driver_ws_id}")

                async_to_sync(channel_layer.group_send)(
                    client_group,
                    {"type": "evt", "event": "ride.cancelled", "payload": payload},
                )
                for g in driver_groups:
                    async_to_sync(channel_layer.group_send)(
                        g,
                        {"type": "evt", "event": "ride.cancelled", "payload": payload},
                    )
                logger.info(
                    "[WS] cancel: notified user.%s & driver.%s ride_id=%s",
                    ride.user_id,
                    list(driver_groups),
                    ride.id,
                )
            except Exception as e:
                logger.exception("WS emit (cancel) failed: %s", e)

        return Response({"id": ride.id, "status": "cancelled"}, status=200)

    # ───────────────────────────────────────────────────────────
    # ARRIVED / START / FINISH
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="arrived")
    def arrived(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.ride_mode == "shared":
            return Response({"detail": "Utilisez les arrêts du trajet partagé."}, status=409)
        if ride.driver_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=403)
        
        # Optionnel: logguer l’heure d’arrivée si le champ existe
        if hasattr(ride, "arrived_at") and not ride.arrived_at:
            ride.arrived_at = timezone.now()
            ride.save(update_fields=["arrived_at"])

        ch = get_channel_layer()
        if ch:
            groups = {user_room(ride.user_id), f"user.{ride.user_id}"}
            payload = {
                "requestId": ride.id,
                "driver": {"id": ride.driver_id},
                "grace": 300,  # 5 minutes
                "at": timezone.now().isoformat(),
            }
            for g in groups:
                # format générique
                async_to_sync(ch.group_send)(g, {
                    "type": "evt",
                    "event": "ride.arrived",
                    "payload": payload,
                })
                # format direct (compat)
                async_to_sync(ch.group_send)(g, {
                    "type": "ride.arrived",
                    "requestId": ride.id,
                    "driverId": ride.driver_id,
                    "grace": 300,
                })
        logger.info("[ARRIVED] ride_id=%s by driver_id=%s -> broadcasting to user.%s",
            ride.id, ride.driver_id, ride.user_id)
        return Response({"ok": True})

    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.ride_mode == "shared":
            return Response({"detail": "Utilisez les arrêts du trajet partagé."}, status=409)
        if ride.driver_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=403)

        if ride.status not in {"accepted", "in_progress"}:
            return Response({"detail": f"Cannot start from status '{ride.status}'"}, status=409)
        if ride.status == "in_progress":
            ch = get_channel_layer()
            if ch:
                payload = {
                    "requestId": ride.id,
                    "driver": {
                        "id": ride.driver_id,
                        "phone_number": getattr(ride.driver, "phone_number", None),
                    },
                    "at": timezone.now().isoformat(),
                    "stopCountdown": True,  # hint explicite pour le front
                }
                for g in {user_room(ride.user_id), f"user.{ride.user_id}"}:
                    async_to_sync(ch.group_send)(g, {"type": "evt", "event": "ride.started", "payload": payload})
                    async_to_sync(ch.group_send)(g, {"type": "ride.started", "requestId": ride.id, "driverId": ride.driver_id})
            return Response({"ok": True, "status": "in_progress"})
        
        ride.status = "in_progress"
        update_fields = ["status"]
        if hasattr(ride, "started_at") and not ride.started_at:
            ride.started_at = timezone.now()
            update_fields.append("started_at")
        ride.save(update_fields=update_fields)

        # push WS au client (2 formats + 2 groupes)
        ch = get_channel_layer()
        if ch:
            groups = {user_room(ride.user_id), f"user.{ride.user_id}"}
            payload = {
                "requestId": ride.id,
                "driver": {
                    "id": ride.driver_id,
                    "phone_number": getattr(ride.driver, "phone_number", None),
                },
                "at": timezone.now().isoformat(),
                "stopCountdown": True,
            }
            for g in groups:
                # format générique
                async_to_sync(ch.group_send)(g, {
                    "type": "evt",
                    "event": "ride.started",
                    "payload": payload,
                })
                # format direct (compat)
                async_to_sync(ch.group_send)(g, {
                    "type": "ride.started",
                    "requestId": ride.id,
                    "driverId": ride.driver_id,
                })
        return Response({"ok": True, "status": "in_progress"})

    @action(detail=True, methods=["post"], url_path="finish")
    def finish(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.ride_mode == "shared":
            return Response({"detail": "Utilisez les arrêts du trajet partagé."}, status=409)
        if not (request.user.is_staff or ride.driver_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=403)
        
        if ride.pause_started_at:
            delta = int((timezone.now() - ride.pause_started_at).total_seconds())
            ride.total_pause_seconds = (ride.total_pause_seconds or 0) + max(0, delta)
            ride.pause_started_at = None

        total_pause_s = _pause_seconds_now(ride)  # (= cumulé désormais)
        fee_int = _compute_pause_fee(total_pause_s)
        ride.pause_fee = Decimal(fee_int)
        base = Decimal(ride.price or 0)
        ride.final_price = base + ride.pause_fee

        ride.status = "completed"
        ride.completed_at = timezone.now()
        ride.save(update_fields=[
            "status", "completed_at",
            "pause_started_at", "total_pause_seconds",
            "pause_fee", "final_price"
        ])

        if not _has_success_payment(ride):
            Payment.objects.get_or_create(
                idempotency_key=f"cash-{ride.id}",
                defaults=dict(
                    ride=ride,
                    amount=ride.final_price,
                    currency="XAF",
                    wallet="CASH",
                    provider="CASH",
                    status="SUCCESS",
                    meta={"source": "finish_auto_cash"},
                )
            )
        if channel_layer:
            payload = {
                "requestId": ride.id,
                "final_price": str(ride.final_price),
                "base_price": str(base),
                "pause_fee": int(ride.pause_fee),
                "total_pause_s": total_pause_s,
            }
            async_to_sync(channel_layer.group_send)(
                user_room(ride.user_id),
                {"type": "evt", "event": "ride.finished", "payload": payload},
            )
            if ride.driver_id:
                async_to_sync(channel_layer.group_send)(
                    driver_room(ride.driver_id),
                    {"type": "evt", "event": "ride.finished", "payload": payload},
                )
        return Response({
            "ok": True,
            "final_price": str(ride.final_price),
            "base_price": str(base),
            "pause_fee": int(ride.pause_fee),
            "total_pause_s": total_pause_s,
        })
    
    @action(detail=True, methods=["get"], url_path="contact")
    def contact(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)

        # sécurité: seul le chauffeur assigné ou staff
        if not (request.user.is_staff or ride.driver_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        # retourne des infos basiques de contact du client
        user = getattr(ride, "user", None) 
        phone = None
        email = None
        first_name = None
        last_name = None

        if user:
            first_name = getattr(user, "first_name", None)
            last_name = getattr(user, "last_name", None)
            phone = (
                getattr(user, "phone_number", None)
                or getattr(user, "phone", None)
            )
            email = getattr(user, "email", None)
            if not phone:
                try:
                    profile = CustomerProfile.objects.filter(user=user).first()
                except Exception:
                    profile = None
                if profile:
                    phone = (
                        getattr(profile, "phone_number", None)
                        or getattr(profile, "phone", None)
                    )
                    if not first_name:
                        first_name = getattr(profile, "first_name", None) or getattr(profile, "name", None)
                    if not last_name:
                        last_name = getattr(profile, "last_name", None)
        payload = {
            "requestId": ride.id,
            "customer": {
                "id": getattr(user, "id", None),
                "first_name": first_name,
                "last_name": last_name,
                "phone_number": phone,   # adapte au nom de champ réel
                "email": email,
            }
        }
        return Response(payload, status=200)
    
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="rate-driver")
    def rate_driver(self, request, pk=None):
        try :
            ride = Ride.objects.select_related('driver').get(pk=pk)
        except Ride.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=404)
        
        if not (request.user.is_staff or ride.user_id == request.user.id):
            return Response({'detail': 'Forbidden (not this ride owner).'}, status=403)
        
        if ride.status != 'completed' and not ride_has_success_payment(ride):
            return Response({'detail': 'Ride is not yet eligible for rating.'}, status=400)
        
        if ride.customer_rating is not None or hasattr(ride, 'driver_rating'):
            return Response({'detail': 'Already rated.'}, status=409)
        
        if ride.driver is None:
            return Response({'detail': 'No driver to rate.'}, status=400)
        
        ser = RateDriverSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        stars = ser.validated_data['rating']
        comment = ser.validated_data.get('comment', '') or ''

        DriverRating.objects.create(
            ride=ride,
            driver=ride.driver,
            passenger=request.user,
            rating=stars,
            comment=comment,
        )

        ride.customer_rating = stars
        ride.customer_comment = comment
        ride.rated_at = timezone.now()
        ride.save(update_fields=['customer_rating', 'customer_comment', 'rated_at'])

        stats, _ = DriverStats.objects.get_or_create(driver=ride.driver)
        new_count = stats.rating_count + 1
        new_avg = ((stats.rating_avg * stats.rating_count) + stars) / new_count
        stats.rating_count = new_count
        stats.rating_avg = new_avg
        stats.save(update_fields=['rating_count', 'rating_avg'])

        return Response({'ok': True, 'rating': stars, 'rating_avg': round(new_avg, 2)}, status=200)
    
    def _driver_is_offline(self, driver_id: int, timeout_s: int = 30) -> bool:
        pres = DriverPresence.objects.filter(driver_id=driver_id).first()
        if not pres:
            return True
        if not pres.is_online:
            return True
        if not pres.last_seen:
            return True
        return (timezone.now() - pres.last_seen).total_seconds() > timeout_s
    
    @action(
        detail=True,
        methods=["post"],
        url_path="force-complete",
        permission_classes=[permissions.IsAdminUser],
    )
    def force_complete(self, request, pk=None):
        """
        Termine une course en cours si le chauffeur est hors-ligne (fail-safe).
        Idempotent: si déjà terminée/annulée → 200 sans effet.
        """
        timeout_s = int(request.data.get("offline_timeout_s", 30))
        with transaction.atomic():
            try:
                ride = Ride.objects.select_for_update().get(pk=pk)
            except Ride.DoesNotExist:
                return Response({'detail': 'Not found.'}, status=404)
            if ride.status in {"completed", "finished", "cancelled"}:
                return Response({"detail": f"Ride already {ride.status}"}, status=200)

            if not ride.driver_id:
                # pas de chauffeur → on peut terminer (ou annuler) selon ta logique
                ride.status = "completed"
                ride.completed_at = timezone.now()
                ride.save(update_fields=["status", "completed_at"])
                return Response({"detail": "Ride completed (no driver assigned)"}, status=200)
            
            if not self._driver_is_offline(ride.driver_id, timeout_s=timeout_s):
                return Response(
                    {"detail": "Driver seems online; refuse fail-safe"},
                    status=status.HTTP_409_CONFLICT,
                )
            # OK, chauffeur offline → on clôture proprement
            if ride.status in ("pending", "accepted"):
                ride.status = "completed"
            elif ride.status == "in_progress":
                ride.status = "completed"
            else:
                ride.status = "completed"

            ride.completed_at = timezone.now()
            ride.save(update_fields=["status", "completed_at"])

        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            ch = get_channel_layer()
            async_to_sync(ch.group_send)(
                f"user.{ride.user_id}",
                {
                    "type": "evt",
                    "event": "ride.completed",
                    "payload": {"rideId": ride.id, "reason": "system_fail_safe"},
                },
            )
        except Exception:
            pass

        return Response({"detail": "Ride force-completed", "ride_id": ride.id}, status=200)
    
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """
        Un chauffeur refuse une offre :
        - la course reste en 'pending'
        - on exclut ce chauffeur du prochain dispatch
        - on renvoie l'offre à d'autres chauffeurs dispo (s'il y en a)
        """
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)

        if ride.status != "pending":
            return Response({"detail": f"Already {ride.status}"}, status=409)

        # On "dés-assigne" la course (au cas où)
        ride.driver = None
        if hasattr(ride, "accepted_at"):
            ride.accepted_at = None
            ride.save(update_fields=["driver", "status", "accepted_at"])
        else:
            ride.save(update_fields=["driver", "status"])

        # Chauffeur qui refuse → on l'exclut des prochains candidats
        rejecting_driver_id = getattr(request.user, "id", None)
        exclude_ids = [rejecting_driver_id] if rejecting_driver_id else []

        pickup_lat = ride.pickup_lat
        pickup_lng = ride.pickup_lng

        if pickup_lat is None or pickup_lng is None:
            # pas de position pickup → on ne peut pas redispatcher proprement
            return Response({"ok": True, "redispatched": False, "reason": "no_pickup_coords"})

        # catégorie demandée pour cette course
        driver_category = getattr(ride, "category", None)
        if driver_category not in ("eco", "clim", "vip"):
            driver_category = None

        # langue du driver (pour le texte dans le payload)
        raw_al = request.headers.get("Accept-Language", "fr")
        lang = (raw_al.split(",")[0].strip()[:5] or "fr") if raw_al else "fr"

        # area : juste un label informatif pour le payload
        area = "city-default"

        # On refait un dispatch mais en excluant ce chauffeur


        # Chauffeur qui refuse → on l'exclut des prochains candidats
        rejecting_driver_id = getattr(request.user, "id", None)
        exclude_ids = [rejecting_driver_id] if rejecting_driver_id else []

        pickup_lat = ride.pickup_lat
        pickup_lng = ride.pickup_lng

        if pickup_lat is None or pickup_lng is None:
            # pas de position pickup → on ne peut pas redispatcher proprement
            return Response({"ok": True, "redispatched": False, "reason": "no_pickup_coords"})

        # catégorie demandée pour cette course
        driver_category = getattr(ride, "category", None)
        if driver_category not in ("eco", "clim", "vip"):
            driver_category = None

        # langue du driver (pour le texte dans le payload, si besoin plus tard)
        raw_al = request.headers.get("Accept-Language", "fr")
        lang = (raw_al.split(",")[0].strip()[:5] or "fr") if raw_al else "fr"

        # area : juste un label informatif pour le payload
        area = "city-default"

        # On relance un dispatch vers un autre chauffeur (si dispo)
        if channel_layer:
            transaction.on_commit(
                lambda: dispatch_ride_offer(
                    ride_id=ride.id,
                    area=area,
                    preferred_category=driver_category or "eco",
                    extra_exclude_driver_ids=exclude_ids,
                )
            )

        return Response({"ok": True, "redispatched": True})
        raw_category = driver_category or "eco"

    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="boost")
    def boost(self, request, pk=None):
        """
        Client accepte un boost pour relancer le dispatch.
        Body: { "boost_amount": 500 }
        """
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)
        if ride.ride_mode == 'shared':
            return Response({'detail': 'Utilisez le trajet partagé sans supplément.'}, status=409)

        # Sécurité : seul le client
        if ride.user_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=403)

        if ride.status != "pending":
            return Response({"detail": f"Cannot boost ride in status '{ride.status}'"}, status=409)

        boost_amount = int(request.data.get("boost_amount", 0))
        if boost_amount <= 0:
            return Response({"detail": "Invalid boost_amount"}, status=400)

        # Anti-spam
        max_attempts = int(getattr(settings, "RIDE_MAX_BOOST_ATTEMPTS", 3))
        if ride.dispatch_attempt >= max_attempts:
            return Response(
                {
                    "detail": "Max boost attempts reached",
                    "dispatch_attempt": ride.dispatch_attempt,
                },
                status=409,
            )

        # Appliquer boost
        ride.boost_amount = boost_amount
        ride.dispatch_status = "searching"
        ride.dispatch_attempt = (ride.dispatch_attempt or 0) + 1
        ride.save(update_fields=["boost_amount", "dispatch_status", "dispatch_attempt"])

        # Relancer dispatch après commit
        area = "city-default"
        preferred_category = ride.category or "eco"

        if channel_layer:
            transaction.on_commit(
                lambda: dispatch_ride_offer(
                    ride_id=ride.id,
                    area=area,
                    preferred_category=preferred_category,
                )
            )

        return Response({
            "ok": True,
            "ride_id": ride.id,
            "boost_amount": ride.boost_amount,
            "dispatch_status": ride.dispatch_status,
            "dispatch_attempt": ride.dispatch_attempt,
        }, status=200)

    @action(detail=False, methods=["get"], url_path="operator-queue", permission_classes=[permissions.IsAdminUser])
    def operator_queue(self, request):
        """
        Courses visibles par l’opérateur :
        - pending : en attente de chauffeur
        - accepted : chauffeur trouvé, course pas encore démarrée
        - in_progress : course en cours
        """
        qs = (
            Ride.objects
            .select_related("user", "driver")
            .filter(status__in=["pending", "accepted", "in_progress"])
            .order_by("-id")
        )

        serializer = OperatorRideSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="operator-stats", permission_classes=[permissions.IsAdminUser])
    def operator_stats(self, request):
        pending_count = Ride.objects.filter(status="pending").count()
        accepted_count = Ride.objects.filter(status="accepted").count()
        in_progress_count = Ride.objects.filter(status="in_progress").count()
        cancelled_count = Ride.objects.filter(status="cancelled").count()
        completed_count = Ride.objects.filter(status="completed").count()

        return Response({
            "pending_count": pending_count,
            "accepted_count": accepted_count,
            "in_progress_count": in_progress_count,
            "cancelled_count": cancelled_count,
            "completed_count": completed_count,
            "active_count": accepted_count + in_progress_count,
        }, status=status.HTTP_200_OK)



class IsRideParticipant(permissions.BasePermission):
    def has_object_permission(self, request, view, obj: Ride):
        uid = getattr(request.user, "id", None)
        return uid and (uid == getattr(obj, "user_id", None) or uid == getattr(obj, "driver_id", None))


class RideDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk: int):
        try:
            r = Ride.objects.get(pk=pk)
        except Ride.DoesNotExist:
            return Response({"detail": "Ride not found."}, status=status.HTTP_404_NOT_FOUND)

        # permission objet (client ou chauffeur)
        if not (request.user.is_staff or request.user.id in (r.user_id, r.driver_id)):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        data = {
            "id": r.id,
            "status": r.status,
            "category": getattr(r, "category", "eco"),
            "price": str(getattr(r, "price", "")),
            "final_price": str(getattr(r, "final_price", "") or ""),
            "pause_fee": int(getattr(r, "pause_fee", 0) or 0),
            "total_pause_s": int(getattr(r, "total_pause_seconds", 0) or 0),
            "pickup": {
                "label": getattr(r, "pickup_location", None),
                "lat": getattr(r, "pickup_lat", None),
                "lng": getattr(r, "pickup_lng", None),
            },
            "dropoff": {
                "label": getattr(r, "dropoff_location", None),
                "lat": getattr(r, "dropoff_lat", None),
                "lng": getattr(r, "dropoff_lng", None),
            },
            "user_id": getattr(r, "user_id", None),
            "driver_id": getattr(r, "driver_id", None),
            "accepted_at": getattr(r, "accepted_at", None),
            "started_at": getattr(r, "started_at", None),
            "completed_at": getattr(r, "completed_at", None),
        }
        return Response(data, status=status.HTTP_200_OK)


class DriverRideLocationView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsDriver]

    def post(self, request, pk):
        ride = get_object_or_404(Ride, pk=pk)

        serializer = DriverLocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lat = serializer.validated_data['lat']
        lng = serializer.validated_data['lng']

        # block si course finie/annulée
        if ride.status in {"cancelled", "completed", "finished"}:
            logger.info("[LOC] reject (legacy) ride_id=%s status=%s", ride.id, ride.status)
            resp = Response(
                {"ok": False, "reason": "ride_ended", "ride_status": ride.status, "ride_id": ride.id},
                status=status.HTTP_410_GONE,
            )
            resp["X-Ride-Ended"] = "1"
            return resp

        ride.driver_lat = lat
        ride.driver_lng = lng
        ride.save(update_fields=['driver_lat', 'driver_lng'])
        if ride.driver_id == request.user.pk:
            from .supervision_presence import record_position
            record_position(request.user.pk, lat, lng)
        return Response({"ok": True})
    
# views.py (extraits)

class MobileInitiate(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Lance un paiement Mobile Money (Airtel/Moov/…).
        Body: { request_id, amount, msisdn, currency? }
        """
        ride = get_object_or_404(Ride, pk=request.data.get("request_id"), user=request.user)
        if hasattr(ride, "pricing"):
            # The signed quote owns the fare; the phone cannot lower the charge.
            if ride.status != "completed":
                return Response({"detail": "La course doit être terminée."}, status=409)
            if _has_success_payment(ride):
                return Response({"detail": "Cette course est déjà enregistrée comme payée."}, status=409)
            amount = Decimal(ride.final_price if ride.final_price is not None else ride.price)
        else:
            amount = Decimal(request.data["amount"])
        msisdn = normalize_msisdn(request.data["msisdn"])

        # Idempotency (header facultatif ; fallback horodaté)
        idem = request.headers.get("Idempotency-Key") or f"pay-{ride.id}-{int(time.time())}"

        p, created = Payment.objects.get_or_create(
            idempotency_key=idem,
            defaults=dict(
                ride=ride,
                amount=amount,
                currency="XAF" if hasattr(ride, "pricing") else request.data.get("currency", "XAF"),
                wallet="MOBILE_MONEY",
                msisdn=msisdn,
                provider=select_provider(ride),
                status="PENDING",
            ),
        )

        if p.ride_id != ride.pk:
            return Response({"detail": "Référence de paiement déjà utilisée."}, status=409)

        # (Re)lancer un paiement seulement à la création ; si existant, on renvoie le tx_id
        if not created and p.provider_txid:
            return Response({"tx_id": p.id, "status": p.status}, status=200)

        ok, provider_txid, message = provider_init_payment(p)  # 🔌 branchement provider réel plus tard
        if not ok:
            p.status = "FAILED"
            p.meta = {"reason": message}
            p.save(update_fields=["status", "meta"])
            return Response({"detail": message}, status=400)

        p.provider_txid = provider_txid
        p.save(update_fields=["provider_txid"])
        return Response({"tx_id": p.id, "status": p.status}, status=201)


class MobileStatus(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Vérifie l’état d’un paiement mobile.
        Query: ?tx_id=...
        """
        tx_id = request.GET.get("tx_id")
        p = Payment.objects.get(pk=tx_id, ride__user=request.user)
        return Response({"status": p.status})


class ProviderCallback(APIView):
    """
    Endpoint appelé par le provider (Airtel/Moov/CinetPay…).
    NB: auth custom (signature/HMAC) gérée par verify_and_parse().
    """
    authentication_classes = []  # on gère la signature nous-mêmes
    permission_classes = []

    def post(self, request):
        ok, provider_txid, provider_status = verify_and_parse(request)  # signature + parsing
        if not ok:
            return Response(status=400)

        p = Payment.objects.filter(provider_txid=provider_txid).first()
        if not p:
            return Response(status=404)

        new_status = map_status(provider_status)  # -> "SUCCESS" / "FAILED" / "PENDING"
        if p.status != new_status:
            p.status = new_status
            p.save(update_fields=["status"])

            # Optionnel : pousser un event WS au client pour MAJ temps réel
            try:
                app_ws_send({
                    "type": "payment.status",
                    "requestId": p.ride_id,
                    "paymentId": p.id,
                    "status": new_status,
                    "amount": str(p.amount),
                })
            except Exception:
                pass

        return Response({"ok": True})
    
class RideStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk: int):
        try:
            ride = Ride.objects.get(pk=pk, user=request.user)
        except Ride.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=404)

        # on expose aussi un “best effort” sur le paiement
        pay = (
            Payment.objects.filter(ride=ride)
            .order_by('-updated_at')
            .first()
        )
        payment_status = pay.status if pay else None
        return Response({'status': ride.status, 'payment_status': payment_status})

class LatestUnratedRideView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # logique : course terminée ET pas encore notée
        qs = (
            Ride.objects
            .filter(user=request.user, status='completed')
            .order_by('-id')
        )
        for r in qs:
            if r.customer_rating is None and not hasattr(r, 'driver_rating'):
                return Response({'id': r.id})
        return Response(status=204)

class RateDriverView(APIView):
    """
    POST /api/rides/<id>/rate-driver/
    body: { "rating": 1..5, "comment": "..." }

    Règle: autorisé si
      - ride.status == 'completed'
      - OU bien un paiement SUCCESS existe (pour le flux “payer puis noter”)
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk: int):
        try:
            ride = Ride.objects.select_related('driver').get(pk=pk, user=request.user)
        except Ride.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=404)

        # conditions d’éligibilité à la notation
        if ride.status != 'completed' and not ride_has_success_payment(ride):
            return Response({'detail': 'Ride is not yet eligible for rating.'}, status=400)

        # déjà notée ?
        if ride.customer_rating is not None or hasattr(ride, 'driver_rating'):
            return Response({'detail': 'Already rated.'}, status=409)

        if ride.driver is None:
            return Response({'detail': 'No driver to rate.'}, status=400)

        ser = RateDriverSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        stars = ser.validated_data['rating']
        comment = ser.validated_data.get('comment', '')

        # création de la note unitaire
        DriverRating.objects.create(
            ride=ride,
            driver=ride.driver,
            passenger=request.user,
            rating=stars,
            comment=comment or '',
        )

        # marquer la ride comme notée
        ride.customer_rating = stars
        ride.customer_comment = comment or ''
        ride.rated_at = timezone.now()
        ride.save(update_fields=['customer_rating', 'customer_comment', 'rated_at'])

        # agrégats driver
        stats, _ = DriverStats.objects.get_or_create(driver=ride.driver)
        new_count = stats.rating_count + 1
        new_avg = ((stats.rating_avg * stats.rating_count) + stars) / new_count
        stats.rating_count = new_count
        stats.rating_avg = new_avg
        stats.save(update_fields=['rating_count', 'rating_avg'])

        return Response({'ok': True, 'rating': stars, 'rating_avg': round(new_avg, 2)})
    
class DriverMeView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        user = request.user
        # stats agrégées
        stats = getattr(user, "driver_stats", None)
        rating_avg = getattr(stats, "rating_avg", 0.0)
        rating_count = getattr(stats, "rating_count", 0)

        # quelques KPI simples
        rides_done = Ride.objects.filter(driver=user, status="completed").count()
        cancels_by_driver = Ride.objects.filter(driver=user, status="cancelled").count()
        accepted = Ride.objects.filter(driver=user).exclude(status="pending").count()
        cancel_rate = (cancels_by_driver / accepted) if accepted else 0.0

        data = {
            "id": user.id,
            "first_name": getattr(user, "first_name", "") or None,
            "last_name": getattr(user, "last_name", "") or None,
            "phone": getattr(user, "phone_number", "") or "",
            "photo_url": None,                # remplis si tu as un champ photo
            "category": None,                 # remplis si tu stockes la catégorie
            "is_online": False,               # branche sur ton statut de présence chauffeur
            "rating_avg": rating_avg,
            "rating_count": rating_count,
            "rides_done": rides_done,
            "cancel_rate": cancel_rate,
        }
        return Response(data, status=200)

class DriverRecentRatingsView(APIView):
    """Optionnel: pour afficher les derniers avis dans un écran dédié."""
    permission_classes = [IsAuthenticated]
    def get(self, request):
        q = (DriverRating.objects
             .filter(driver=request.user)
             .order_by("-created_at")[:10])
        items = [{
            "ride_id": r.ride_id,
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at.isoformat(),
        } for r in q]
        return Response({"results": items}, status=200)
    
class DriverVehicleMe(APIView):
    """
    GET  /driver/vehicle/ -> infos véhicule "principal" du chauffeur
    PUT  /driver/vehicle/ -> création/mise à jour
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        v = RideVehicle.objects.filter(driver=request.user).order_by('id').first()
        if not v:
            return Response({}, status=200)
        return Response(DriverVehicleMeSerializer(v).data, status=200)

    @transaction.atomic
    def put(self, request):
        from .models import SharedTrip
        from django.contrib.auth import get_user_model
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if any(key in request.data for key in ('shared_enabled', 'passenger_capacity')):
            from drivers.models import Driver
            if not Driver.objects.filter(user=request.user, is_blocked=False).exists():
                return Response({'detail': 'Compte chauffeur requis.'}, status=403)
        if SharedTrip.objects.filter(driver=request.user, state='active').exists():
            return Response({'detail': 'Terminez le trajet partagé avant de modifier le véhicule.'}, status=409)
        v = RideVehicle.objects.filter(driver=request.user).order_by('id').first()
        if not v:
            # Valeurs par défaut minimales pour éviter les NOT NULL
            v = RideVehicle(
                driver=request.user,
                brand='',
                model='',
                category='eco',
                city='-',
                latitude=0.0,
                longitude=0.0,
            )
        ser = DriverVehicleMeSerializer(v, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(driver=request.user)  # force le driver au cas où
        return Response(DriverVehicleMeSerializer(v).data, status=200)
    
class DriverNavEventViewSet(mixins.CreateModelMixin,
                            mixins.ListModelMixin,
                            mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    queryset = DriverNavEvent.objects.select_related("driver").all()
    serializer_class = DriverNavEventSerializer
    permission_classes = [IsAuthenticated & IsDriverOrStaff]

    def perform_create(self, serializer):
        serializer.save(driver=self.request.user)

    def get_queryset(self):
        qs = super().get_queryset()
        rid   = self.request.query_params.get("request_id")
        etype = self.request.query_params.get("event_type")
        since = self.request.query_params.get("since")  # ISO date/time
        driver_id = self.request.query_params.get("driver")

        if rid: qs = qs.filter(request_id=rid)
        if etype: qs = qs.filter(event_type=etype)
        if since: qs = qs.filter(created_at__gte=since)
        if driver_id: qs = qs.filter(driver_id=driver_id)

        u = self.request.user
        if not getattr(u, "is_staff", False):
            qs = qs.filter(driver=u)
        return qs

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        events = request.data.get("events", [])
        if not isinstance(events, list):
            return Response({"detail": "events must be a list"}, status=400)

        created = []
        for data in events:
            ser = self.get_serializer(data=data)
            if ser.is_valid():
                created.append(DriverNavEvent(driver=request.user, **ser.validated_data))
        if created:
            DriverNavEvent.objects.bulk_create(created, batch_size=500)
        return Response({"created": len(created)}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        """
        GET /api/ridevtc/driver-nav/events/metrics/?since=2025-10-01&driver=123&request_id=456
        Renvoie: compte par type, total, série horaire.
        """
        qs = self.get_queryset()
        # Agrégats
        by_type = qs.values("event_type").annotate(n=Count("id")).order_by("-n")
        total = qs.count()
        # Série horaire (events/h)
        per_hour = (
            qs.annotate(h=TruncHour("created_at"))
              .values("h")
              .annotate(n=Count("id"))
              .order_by("h")
        )

        return Response({
            "total": total,
            "by_type": list(by_type),
            "per_hour": [{"hour": row["h"], "count": row["n"]} for row in per_hour],
        })

class DriverPresenceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        raw_type = (getattr(user, "user_type", "") or "").lower()

        if raw_type not in ("chauffeur", "driver"):
            return Response({"detail": "Not a driver"}, status=403)

        online = bool(request.data.get("online", True))
        lat = request.data.get("lat")
        lng = request.data.get("lng")

        pres, _ = DriverPresence.objects.get_or_create(driver=user)
        pres.is_online = online
        pres.last_seen = timezone.now()

        if lat is not None:
            pres.lat = lat
        if lng is not None:
            pres.lng = lng

        city = request.data.get("city")
        if city:
            pres.city = city

        pres.save()
        if lat is not None and lng is not None:
            from .supervision_presence import record_position
            record_position(user.pk, lat, lng)

        return Response({"ok": True})