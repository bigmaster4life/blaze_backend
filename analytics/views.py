# analytics/views.py
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime

from django.db.models import Sum, Count, Value, DecimalField, Q, F
from decimal import Decimal
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date

from rest_framework.viewsets import ViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response

from RideVTC.models import Ride, Payment
from vehicles.models import Rental

ZERO_MONEY = Value(Decimal('0'), output_field=DecimalField(max_digits=12, decimal_places=2))
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


class AnalyticsViewSet(ViewSet):
    """
    ViewSet DRF exposant:
      - GET /api/admin/analytics/summary/
      - GET /api/admin/analytics/timeseries/?metric=rides_per_hour&day=YYYY-MM-DD
      - GET /api/admin/analytics/revenue_daily/?from=YYYY-MM-DD&to=YYYY-MM-DD
      - GET /api/admin/analytics/payment_split/?from=YYYY-MM-DD&to=YYYY-MM-DD
      - GET /api/admin/analytics/top_drivers/?from=YYYY-MM-DD&to=YYYY-MM-DD&limit=10
      - GET /api/admin/analytics/issues/?limit=30
      - GET /api/admin/analytics/live/?limit=30
    """
    permission_classes = [IsAuthenticated]

    # ───────────────── helpers ─────────────────

    def _date_range(self, request):
        """Construit un [start, end] (aware) à partir de ?from&?to (YYYY-MM-DD)."""
        try:
            to_s = request.query_params.get("to")
            from_s = request.query_params.get("from")
            tz = timezone.get_current_timezone()

            if to_s:
                to_d = parse_date(to_s) or timezone.localdate()
            else:
                to_d = timezone.localdate()

            if from_s:
                from_d = parse_date(from_s) or (to_d - timedelta(days=30))
            else:
                from_d = to_d - timedelta(days=30)

            start = timezone.make_aware(datetime.combine(from_d, dtime.min), tz)
            end = timezone.make_aware(datetime.combine(to_d, dtime.max), tz)
            return start, end
        except Exception:
            now = timezone.now()
            return now - timedelta(days=30), now

    def _city_filter(self, request):
        city = (request.query_params.get("city") or "").strip()
        return city or None

    # ───────────────── endpoints ─────────────────

    @action(detail=False, methods=["get"])
    def summary(self, request):
        start, end = self._date_range(request)
        city = self._city_filter(request)

        ride_qs = Ride.objects.filter(requested_at__range=(start, end))
        rental_qs = Rental.objects.filter(created_at__range=(start, end))

        if city:
            # Ajuste si ton modèle diffère
            ride_qs = ride_qs.filter(
                Q(vehicle__city__iexact=city) |
                Q(user__email__isnull=False)  # garde un fallback (évite filtre vide)
            )
            rental_qs = rental_qs.filter(vehicle__city__iexact=city)

        rides_live = ride_qs.filter(status__in=["accepted", "in_progress"]).count()
        rides_waiting_pickup = ride_qs.filter(
            status="accepted", accepted_at__isnull=False, driver_lat__isnull=False
        ).count()
        rides_completed = ride_qs.filter(status="completed").count()

        total_rides = ride_qs.count()
        canceled = ride_qs.filter(status__in=["cancelled", "rejected"]).count()
        cancel_rate = (canceled / total_rides) if total_rides else 0.0

        # moyennes (seconds) safe
        def _secs(a, b):
            if not a or not b:
                return None
            return max(0, int((b - a).total_seconds()))

        pickups, durations = [], []
        for r in ride_qs.only("requested_at", "accepted_at", "completed_at"):
            s1 = _secs(r.requested_at, r.accepted_at)
            if s1 is not None:
                pickups.append(s1)
            s2 = _secs(r.accepted_at, r.completed_at)
            if s2 is not None:
                durations.append(s2)

        avg_pickup = int(sum(pickups) / len(pickups)) if pickups else 0
        avg_duration = int(sum(durations) / len(durations)) if durations else 0

        rentals_active = Rental.objects.filter(
            status__in=["pending", "confirmed", "in_progress"]
        ).count()

        pay_qs = Payment.objects.filter(
            ride__requested_at__range=(start, end), status="SUCCESS"
        )
        if city:
            pay_qs = pay_qs.filter(ride__vehicle__city__iexact=city)

        gmv_dec = pay_qs.aggregate(
            v=Coalesce(Sum("amount"), ZERO_MONEY, output_field=MONEY_FIELD)
        )["v"] or Decimal("0")
        gmv = float(gmv_dec)
        drivers_earnings_dec = pay_qs.aggregate(
            v=Coalesce(Sum(F("amount") * Decimal("0.80")), ZERO_MONEY, output_field=MONEY_FIELD)
        )["v"] or Decimal("0")
        commission_dec = pay_qs.aggregate(
            v=Coalesce(Sum(F("amount") * Decimal("0.20")), ZERO_MONEY, output_field=MONEY_FIELD)
        )["v"] or Decimal("0")

        return Response({
            "rides_live": rides_live,
            "rides_waiting_pickup": rides_waiting_pickup,
            "rides_completed": rides_completed,
            "cancel_rate": cancel_rate,
            "avg_pickup_time_sec": avg_pickup,
            "avg_ride_duration_sec": avg_duration,
            "rentals_active": rentals_active,
            "incidents_last_hour": 0,
            "tickets_open": 0,
            "gmv": gmv,
            "drivers_earnings": float(drivers_earnings_dec),
            "platform_commission": float(commission_dec),
        })

    @action(detail=False, methods=["get"])
    def timeseries(self, request):
        metric = request.query_params.get("metric") or "rides_per_hour"
        day_s = request.query_params.get("day")
        city = self._city_filter(request)

        tz = timezone.get_current_timezone()
        day = parse_date(day_s) if day_s else timezone.localdate()
        if not day:
            day = timezone.localdate()

        start = timezone.make_aware(datetime.combine(day, dtime.min), tz)
        end = timezone.make_aware(datetime.combine(day, dtime.max), tz)

        qs = Ride.objects.filter(requested_at__range=(start, end))
        if city:
            qs = qs.filter(Q(vehicle__city__iexact=city))

        buckets = {f"{h:02d}:00": 0 for h in range(24)}
        for r in qs.only("requested_at"):
            h = r.requested_at.astimezone(tz).strftime("%H:00")
            buckets[h] = buckets.get(h, 0) + 1

        out = [{"t": f"{day}T{h}:00:00", "rides": buckets[h]} for h in sorted(buckets.keys())]
        return Response(out)

    @action(detail=False, methods=["get"])
    def revenue_daily(self, request):
        start, end = self._date_range(request)
        city = self._city_filter(request)
        tz = timezone.get_current_timezone()

        pay_qs = Payment.objects.filter(
            ride__requested_at__range=(start, end), status="SUCCESS"
        )
        if city:
            pay = pay.filter(ride__vehicle__city__iexact=city)

        qs = (
            pay_qs
            .annotate(d=TruncDate("ride__requested_at"))
            .values("d")
            .annotate(
                gmv=Coalesce(Sum("amount"), ZERO_MONEY, output_field=MONEY_FIELD),
                commission=Coalesce(Sum(F("amount") * Decimal("0.20")), ZERO_MONEY, output_field=MONEY_FIELD),
            )
            .order_by("d")
        )
        data = [
            {
                "d": row["d"].isoformat() if hasattr(row["d"], "isoformat") else str(row["d"]),
                "gmv": float(row["gmv"] or Decimal("0")),
                "commission": float(row["commission"] or Decimal("0")),
            }
            for row in qs
        ]
        return Response(data)

    @action(detail=False, methods=["get"])
    def payment_split(self, request):
        start, end = self._date_range(request)
        city = self._city_filter(request)

        qs = Payment.objects.filter(
            ride__requested_at__range=(start, end), status="SUCCESS"
        )
        if city:
            qs = qs.filter(ride__vehicle__city__iexact=city)

        split = {"cash": 0.0, "mobile_money": 0.0, "wallet": 0.0}
        for p in qs.only("wallet", "amount"):
            w = (p.wallet or "").upper()
            amt = float(p.amount or 0)
            if w == "BLAZE":
                split["wallet"] += amt
            elif w == "MOBILE_MONEY":
                split["mobile_money"] += amt
            else:
                split["cash"] += amt
        return Response(split)

    @action(detail=False, methods=["get"])
    def top_drivers(self, request):
        start, end = self._date_range(request)
        city = self._city_filter(request)
        limit = int(request.query_params.get("limit") or 10)

        rides = Ride.objects.filter(
            requested_at__range=(start, end), status="completed"
        ).select_related("driver")
        if city:
            rides = rides.filter(Q(vehicle__city__iexact=city))

        pays = Payment.objects.filter(ride__in=rides, status="SUCCESS").select_related("ride")
        pay_map = defaultdict(float)
        for p in pays:
            pay_map[p.ride_id] += float(p.amount or 0)

        stats = defaultdict(lambda: {"rides": 0, "revenue": 0.0, "name": "—", "rating": 5.0})
        for r in rides:
            did = r.driver_id or 0
            stats[did]["rides"] += 1
            stats[did]["name"] = (r.driver.get_full_name() if r.driver else "—") or (r.driver.email if r.driver else "—")
            stats[did]["revenue"] += pay_map.get(r.id, 0.0) * 0.8

        rows = sorted(
            [{"id": did or 0, **val} for did, val in stats.items()],
            key=lambda x: x["revenue"],
            reverse=True
        )[:limit]
        return Response(rows)

    @action(detail=False, methods=["get"])
    def issues(self, request):
        limit = int(request.query_params.get("limit") or 30)
        return Response([][:limit])

    @action(detail=False, methods=["get"])
    def live(self, request):
        limit = int(request.query_params.get("limit") or 30)
        return Response([][:limit])