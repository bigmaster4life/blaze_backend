from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from .models import Ride


class HistorySerializer(serializers.ModelSerializer):
    driver_revenue = serializers.SerializerMethodField()
    shared_discount_percent = serializers.SerializerMethodField()

    def get_shared_discount_percent(self, ride):
        if hasattr(ride, 'shared_booking'):
            return float(ride.shared_booking.discount_rate * 100)
        return None

    def get_driver_revenue(self, ride):
        request = self.context.get("request")
        if not request or request.query_params.get("role") != "driver":
            return None
        values = [p.allocation.driver_amount for p in ride.payments.all()
                  if p.status == "SUCCESS" and hasattr(p, "allocation")]
        return str(sum(values)) if values else None

    class Meta:
        model = Ride
        fields = ("id", "status", "pickup_location", "dropoff_location", "price",
                  "final_price", "requested_at", "completed_at", "driver_revenue", "ride_mode", "passenger_count", "shared_discount_percent")


class HistoryPagination(PageNumberPagination):
    page_size = 20


class RideHistoryView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = HistorySerializer
    pagination_class = HistoryPagination

    def get_queryset(self):
        role = self.request.query_params.get("role", "customer")
        if role not in ("customer", "driver"):
            raise ValidationError({"role": "Rôle invalide."})
        owner_field = "driver_id" if role == "driver" else "user_id"
        return Ride.objects.filter(
            **{owner_field: self.request.user.pk},
            status__in=("completed", "cancelled", "rejected"),
        ).select_related("shared_booking").prefetch_related("payments__allocation").order_by("-requested_at", "-id")
