# RideVTC/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import RideVehicleViewSet, RideViewSet, RateDriverView, RideStatusView, LatestUnratedRideView, DriverMeView, DriverRecentRatingsView, DriverVehicleMe
from RideVTC.views import MobileInitiate, MobileStatus
from RideVTC.callbacks import ProviderCallback
from RideVTC.views import DriverPresenceView

from .quotes import RideQuoteView, RideQuoteMenuView, RideOptionsView
from .shared_views import SharedTripView, SharedPositionView, SharedStopView, SharedCurrentView
from .history import RideHistoryView

router = DefaultRouter()
router.register(r'ride-vehicles', RideVehicleViewSet, basename='ridevehicle')
router.register(r"rides", RideViewSet, basename="rides")

urlpatterns = [
    path("rides/options/", RideOptionsView.as_view(), name="ride-options"),
    path("shared-trips/current/", SharedCurrentView.as_view(), name="shared-current"),
    path("shared-trips/<int:pk>/", SharedTripView.as_view(), name="shared-trip"),
    path("shared-trips/<int:pk>/location/", SharedPositionView.as_view(), name="shared-position"),
    path("shared-trips/<int:pk>/stop/", SharedStopView.as_view(), name="shared-stop"),
    path("rides/quotes/", RideQuoteMenuView.as_view(), name="ride-quotes"),
    path("rides/quote/", RideQuoteView.as_view(), name="ride-quote"),
    path("rides/history/", RideHistoryView.as_view(), name="ride-history"),
    path('', include(router.urls)),
    path("payments/mobile/initiate/", MobileInitiate.as_view(), name="mobile-init"),
    path("payments/mobile/status/", MobileStatus.as_view(), name="mobile-status"),
    path("payments/mobile/callback/", ProviderCallback.as_view(), name="mobile-callback"),
    path('rides/<int:pk>/status/', RideStatusView.as_view(), name='ride-status'),
    path('rides/latest-unrated/', LatestUnratedRideView.as_view(), name='ride-latest-unrated'),
    path("driver/me/", DriverMeView.as_view(), name="driver-me"),
    path("driver/ratings/recent/", DriverRecentRatingsView.as_view(), name="driver-ratings-recent"),
    path('driver/vehicle/', DriverVehicleMe.as_view(), name='driver-vehicle-me'),
    path("ridevtc/driver/presence/", DriverPresenceView.as_view(), name="driver-presence"),

]