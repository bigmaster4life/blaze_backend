from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from .models import Ride


class HistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Ride
        fields = ("id", "status", "pickup_location", "dropoff_location", "price",
                  "final_price", "requested_at", "completed_at")


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
        ).order_by("-requested_at", "-id")
