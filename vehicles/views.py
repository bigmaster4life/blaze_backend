from rest_framework import viewsets, filters, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Vehicle, Promo, Rental, set_vehicle_availability, VehicleBooking, generate_ident_code
from rest_framework import permissions, status
from rest_framework.decorators import action
from django.db import transaction, models
from django.db.models import Exists, OuterRef, Q
from django.utils.dateparse import parse_datetime
from .serializers import VehicleSerializer, PromoSerializer, RentalSerializer
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from django.utils import timezone

class VehicleViewSet(viewsets.ModelViewSet):
    serializer_class = VehicleSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [filters.SearchFilter]
    search_fields = ['city', 'category']

    def get_queryset(self):
        qs = Vehicle.objects.all().order_by('-created_at')
        city = self.request.query_params.get('city')
        category = self.request.query_params.get('category')
        if city:
            qs = qs.filter(city__iexact=city)
        if category:
            qs = qs.filter(category__iexact=category)
        
        start = self.request.query_params.get('start')
        end = self.request.query_params.get('end')
        if start and end:
            start_dt = parse_datetime(start)
            end_dt = parse_datetime(end)
            if start_dt and end_dt:
                overlap = VehicleBooking.objects.filter(
                    vehicle=OuterRef('pk'),
                    status='CONFIRMED'
                ).filter(
                    Q(start_at__lt=end_dt) & Q(end_at__gt=start_dt)
                )
                qs = qs.annotate(has_overlap=Exists(overlap))
                qs = qs.annotate(
                    is_available_dyn=models.Case(
                        models.When(has_overlap=True, then=models.Value(False)),
                        default=models.F('is_available'),
                        output_field=models.BooleanField(),
                    )
                )
        return qs

    def get_serializer_context(self):
        # pour build_absolute_uri de l'image + tout champ dérivé du request
        ctx = super().get_serializer_context()
        ctx['request'] = self.request
        return ctx

    def perform_create(self, serializer):
        # attribue automatiquement le propriétaire connecté
        serializer.save(owner=self.request.user)

class RentalPromoView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        city = request.query_params.get("city", "").strip()
        qs = Promo.objects.active(now=timezone.now()).for_city(city).order_by("-priority")
        promo = qs.first()
        if not promo:
            # renvoyer {} → ton écran appliquera un fallback local
            return Response({})
        ser = PromoSerializer(promo, context={"request": request})
        return Response(ser.data)

class RentalViewSet(viewsets.ModelViewSet):
    """
    /api/rentals/ :
      - POST create (pending + hold)
      - POST {id}/confirm_cash/
      - POST {id}/start/
      - POST {id}/finish/
      - POST {id}/cancel/
      - POST {id}/extend/
    """
    serializer_class = RentalSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = Rental.objects.select_related('vehicle', 'user').all()

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_staff:
            return qs
        # un user voit ses rentals
        return qs.filter(user=self.request.user)

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def confirm_cash(self, request, pk=None):
        r = self.get_object()
        if r.status not in ['pending', 'confirmed']:
            return Response({'detail': 'Statut incompatible.'}, status=400)
        r.status = 'confirmed'
        r.payment_method = 'cash'
        if not r.identification_code:
            r.identification_code = generate_ident_code()
        if not r.total_amount or str(r.total_amount) == '0':
            r.recompute_total()
        r.save(update_fields=['status', 'payment_method', 'identification_code', 'total_amount'])
        set_vehicle_availability(r.vehicle)
        return Response({
            'ok': True,
            'status': r.status,
            'identification_code': r.identification_code,
            'total_amount': r.total_amount,
        })

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        r = self.get_object()
        if r.status not in ['confirmed', 'in_progress']:
            return Response({'detail': 'Statut incompatible.'}, status=400)
        r.status = 'in_progress'

        if not r.identification_code:
            r.identification_code = generate_ident_code()
        
        if not r.total_amount or str(r.total_amount) == '0':
            r.recompute_total()
        
        r.save(update_fields=['status', 'identification_code', 'total_amount'])
        set_vehicle_availability(r.vehicle)
        return Response({
            'ok': True,
            'status': r.status,
            'identification_code': r.identification_code,
            'total_amount': r.total_amount,
        })

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def finish(self, request, pk=None):
        r = self.get_object()
        if r.status not in ['in_progress', 'confirmed']:
            return Response({'detail': 'Statut incompatible.'}, status=400)
        r.status = 'finished'
        r.save(update_fields=['status'])
        set_vehicle_availability(r.vehicle)  # → redevient disponible (vert) si plus d’actifs
        return Response({'ok': True, 'status': r.status})

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        r = self.get_object()
        if r.status in ['finished', 'canceled', 'expired']:
            return Response({'detail': 'Déjà terminée/annulée.'}, status=400)
        r.status = 'canceled'
        r.save(update_fields=['status'])
        set_vehicle_availability(r.vehicle)
        return Response({'ok': True, 'status': r.status})

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def extend(self, request, pk=None):
        """
        Payload attendu: { "new_end_date": "2025-01-31T12:00:00Z", "payment_method": "cash|wallet|mobile" }
        """
        r = self.get_object()
        new_end = request.data.get('new_end_date')
        if not new_end:
            return Response({'detail': 'new_end_date requis.'}, status=400)

        try:
            # si tu utilises USE_TZ=True, parse en aware
            new_end_dt = timezone.datetime.fromisoformat(new_end.replace('Z', '+00:00'))
        except Exception:
            return Response({'detail': 'Format de date invalide.'}, status=400)

        if new_end_dt <= r.end_date:
            return Response({'detail': 'La nouvelle fin doit être > à l’ancienne.'}, status=400)

        # anti-chevauchement
        conflicts = Rental.objects.filter(
            vehicle=r.vehicle,
            status__in=['pending', 'confirmed', 'in_progress'],
        ).exclude(pk=r.pk).filter(
            start_date__lt=new_end_dt,
            end_date__gt=r.start_date,
        ).exists()
        if conflicts:
            return Response({'detail': 'Chevauchement: véhicule déjà réservé sur ce créneau.'}, status=409)

        r.end_date = new_end_dt
        pm = request.data.get('payment_method')
        if pm in dict(Rental.PAY_CHOICES):
            r.payment_method = pm
        r.save(update_fields=['end_date', 'payment_method'])
        set_vehicle_availability(r.vehicle)
        return Response({'ok': True, 'status': r.status, 'end_date': r.end_date})