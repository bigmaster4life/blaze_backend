# RideVTC/views.py

from rest_framework.viewsets import ModelViewSet
from rest_framework.views import APIView
from .models import RideVehicle, Ride, Payment, DriverStats, DriverRating
from .serializers import (
    RideVehicleSerializer,
    RideCreateSerializer,
    RideSerializer,
    RideOutSerializer,
    DriverLocationSerializer,
    RateDriverSerializer,
    DriverVehicleMeSerializer
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status, permissions
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
from RideVTC.utils.payloads import build_ride_offer_payload
import re
import logging
import time
from decimal import Decimal

from asgiref.sync import async_to_sync
try:
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
except Exception:
    channel_layer = None

from .ws import app_ws_send  # mock WS; remplace par ta vraie intégration si dispo

logger = logging.getLogger(__name__)

def ride_has_success_payment(ride: Ride) -> bool:
    return Payment.objects.filter(ride=ride, status='SUCCESS').exists()



class IsDriver(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and getattr(user, 'is_driver', False))


class RideVehicleViewSet(ModelViewSet):
    serializer_class = RideVehicleSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = RideVehicle.objects.filter(available=True)
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

    def get_serializer_class(self):
        return RideCreateSerializer if self.action in ("create", "create_alias") else RideSerializer

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
            data["driver"] = {
                "id": ride.driver_id,
                "email": getattr(d, "email", None),
                "first_name": getattr(d, "first_name", None),
                "last_name": getattr(d, "last_name", None),
            }
        return Response(data, status=status.HTTP_200_OK)

    # ───────────────────────────────────────────────────────────
    # DRIVER → position → client
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="location")
    def location(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
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
        ride: Ride = ser.save(status="pending", accepted_at=None, completed_at=None)

        def _clean(s: str) -> str:
            return re.sub(r"[^0-9A-Za-z_.-]", "_", (s or "").strip().lower())[:50]

        category = _clean(request.data.get("category") or "eco")
        area = _clean(request.data.get("area") or "city-default")

        if channel_layer:
            raw_al = request.headers.get("Accept-Language", "fr")
            lang = (raw_al.split(",")[0].strip()[:5] or "fr") if raw_al else "fr"

            payload = build_ride_offer_payload(
                ride,
                category=category,
                area=area,
                language=lang,
            )
            room = pool_room(category, area)  # ex: "pool.eco.city-default"
            async_to_sync(channel_layer.group_send)(
                room,
                {"type": "ride.requested", "ride": payload}
            )
            logger.info("[WS] sent ride.requested → group=%s ride_id=%s", room, ride.id)

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
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)

        if ride.status != "pending":
            return Response({"detail": f"Already {ride.status}"}, status=409)

        # Chauffeur authentifié
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

                        # ✅ ajouts lisibles pour le client aussi
                        "pickup_label": (ride.pickup_location or None),
                        "dropoff_label": (ride.dropoff_location or None),
                        "price_text": f"{int(ride.price)} FCFA" if ride.price is not None else "—",
                    },
                }
                # 1) Format générique (relay via AppConsumer.evt → {event, payload})
                evt_msg = {"type": "evt", "event": "ride.accepted", "payload": payload}
                async_to_sync(channel_layer.group_send)(client_group, evt_msg)
                async_to_sync(channel_layer.group_send)(alt_group,    evt_msg)

                # 2) Format direct (certains front écoutent msg.type)
                direct_msg = {
                    "type": "ride.accepted",
                    "requestId": ride.id,
                    "driver": payload["driver"],
                    "ride": payload["ride"],
                }
                async_to_sync(channel_layer.group_send)(client_group, direct_msg)
                async_to_sync(channel_layer.group_send)(alt_group,    direct_msg)
                # (facultatif) notifier aussi le chauffeur affecté (canal privé)
                async_to_sync(channel_layer.group_send)(
                    driver_room(ride.driver_id),
                    {"type": "evt", "event": "ride.assigned", "payload": {"requestId": ride.id}},
                )
                logger.info("[WS] accept: notified %s & %s ride_id=%s", client_group, alt_group, ride.id)
            except Exception:
                logger.exception("WS emit (accept) failed")

        return Response({"ok": True})

    # ───────────────────────────────────────────────────────────
    # CANCEL → status + push à client & chauffeur
    # ───────────────────────────────────────────────────────────
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        ride = get_object_or_404(Ride.objects.select_for_update(), pk=pk)

        is_customer = (ride.user_id == request.user.id)
        is_driver = (ride.driver_id == request.user.id)
        if not (is_customer or is_driver or request.user.is_staff):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        if ride.status in {"cancelled", "completed", "finished"}:
            return Response({"id": ride.id, "status": ride.status}, status=200)

        ride.status = "cancelled"
        if hasattr(ride, "cancelled_at"):
            ride.cancelled_at = timezone.now()
            ride.save(update_fields=["status", "cancelled_at"])
        else:
            ride.save(update_fields=["status"])

        if channel_layer:
            try:
                payload = {"requestId": ride.id}
                # Client (toujours)
                async_to_sync(channel_layer.group_send)(
                    user_room(ride.user_id),
                    {"type": "evt", "event": "ride.cancelled", "payload": payload},
                )
                # Chauffeur si assigné
                if ride.driver_id:
                    async_to_sync(channel_layer.group_send)(
                        driver_room(ride.driver_id),
                        {"type": "evt", "event": "ride.cancelled", "payload": payload},
                    )
                logger.info("[WS] cancel: notified user.%s & driver.%s ride_id=%s",
                            ride.user_id, ride.driver_id, ride.id)
            except Exception as e:
                logger.exception("WS emit (cancel) failed: %s", e)

        return Response({"id": ride.id, "status": "cancelled"}, status=200)

    # ───────────────────────────────────────────────────────────
    # ARRIVED / START / FINISH
    # ───────────────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="arrived")
    def arrived(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
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
        if ride.driver_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=403)
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
                "driver": {"id": ride.driver_id},
                "at": timezone.now().isoformat(),
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
        return Response({"ok": True})

    @action(detail=True, methods=["post"], url_path="finish")
    def finish(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)
        if ride.driver_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=403)
        ride.status = "completed"
        ride.completed_at = timezone.now()
        ride.save(update_fields=["status", "completed_at"])
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                user_room(ride.user_id),
                {"type": "evt", "event": "ride.finished", "payload": {"requestId": ride.id}},
            )
        return Response({"ok": True})
    
    @action(detail=True, methods=["get"], url_path="contact")
    def contact(self, request, pk=None):
        ride = get_object_or_404(Ride, pk=pk)

        # sécurité: seul le chauffeur assigné ou staff
        if not (request.user.is_staff or ride.driver_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        # retourne des infos basiques de contact du client
        user = getattr(ride, "user", None)
        payload = {
            "requestId": ride.id,
            "customer": {
                "id": getattr(user, "id", None),
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "phone": getattr(user, "phone", None),   # adapte au nom de champ réel
                "email": getattr(user, "email", None),
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
        return Response({"ok": True})
    
# views.py (extraits)

class MobileInitiate(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Lance un paiement Mobile Money (Airtel/Moov/…).
        Body: { request_id, amount, msisdn, currency? }
        """
        ride = Ride.objects.get(pk=request.data["request_id"], user=request.user)
        amount = Decimal(request.data["amount"])
        msisdn = normalize_msisdn(request.data["msisdn"])

        # Idempotency (header facultatif ; fallback horodaté)
        idem = request.headers.get("Idempotency-Key") or f"pay-{ride.id}-{int(time.time())}"

        p, created = Payment.objects.get_or_create(
            idempotency_key=idem,
            defaults=dict(
                ride=ride,
                amount=amount,
                currency=request.data.get("currency", "XAF"),
                wallet="MOBILE_MONEY",
                msisdn=msisdn,
                provider=select_provider(ride),
                status="PENDING",
            ),
        )

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
        v = RideVehicle.objects.filter(driver=request.user).order_by('id').first()
        if not v:
            # Valeurs par défaut minimales pour éviter les NOT NULL
            v = RideVehicle(
                driver=request.user,
                brand='',
                model='',
                category='Berline',
                city='-',
                latitude=0.0,
                longitude=0.0,
            )
        ser = DriverVehicleMeSerializer(v, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(driver=request.user)  # force le driver au cas où
        return Response(DriverVehicleMeSerializer(v).data, status=200)