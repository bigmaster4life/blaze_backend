from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from .pricing import QuoteInput, make_quote, route_metrics, RATES

class QuoteThrottle(UserRateThrottle):
    rate = '20/min'

class RideQuoteView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [QuoteThrottle]

    def post(self, request):
        serializer = QuoteInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(make_quote(request.user.pk, serializer.validated_data))


class RideQuoteMenuView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [QuoteThrottle]

    def post(self, request):
        payload = request.data.copy()
        payload['category'] = 'eco'
        serializer = QuoteInput(data=payload)
        serializer.is_valid(raise_exception=True)
        metrics = route_metrics(serializer.validated_data)
        return Response({'quotes': [make_quote(request.user.pk,
            {**serializer.validated_data, 'category': category}, metrics)
            for category in RATES]})


class RideOptionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.conf import settings
        return Response({'shared_enabled': getattr(settings, 'VTC_SHARED_ENABLED', False),
                         'max_passengers': 4, 'max_detour_seconds': getattr(settings, 'VTC_SHARED_MAX_DETOUR_SECONDS', 300)})
