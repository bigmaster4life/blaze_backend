from django.shortcuts import get_object_or_404
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import serializers
from .models import SharedTrip
from .shared import trip_data, record_position, complete_stop, match_waiting

class SharedPosition(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    def validate(self, attrs):
        import math
        if not all(math.isfinite(v) for v in attrs.values()):
            raise serializers.ValidationError('Position invalide.')
        return attrs

class SharedStopInput(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    ride_id = serializers.IntegerField(min_value=1)
    kind = serializers.ChoiceField(choices=['pickup','dropoff'])

class SharedTripView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, pk):
        trip = get_object_or_404(SharedTrip, pk=pk, driver=request.user)
        return Response(trip_data(trip))

class SharedPositionView(APIView):
    permission_classes = [IsAuthenticated]
    @transaction.atomic
    def post(self, request, pk):
        trip = get_object_or_404(SharedTrip.objects.select_for_update(), pk=pk, driver=request.user, state='active')
        serializer = SharedPosition(data=request.data)
        serializer.is_valid(raise_exception=True)
        first_sample = trip.last_location_at is None
        record_position(trip, **serializer.validated_data)
        if first_sample:
            transaction.on_commit(lambda: match_waiting(trip.pk), robust=True)
        return Response({'ok':True})

class SharedStopView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request, pk):
        get_object_or_404(SharedTrip, pk=pk, driver=request.user)
        serializer = SharedStopInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        trip = complete_stop(pk, request.user, **serializer.validated_data)
        return Response(trip_data(trip))

class SharedCurrentView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        trip = SharedTrip.objects.filter(driver=request.user, state='active').first()
        return Response(trip_data(trip)) if trip else Response(status=204)
