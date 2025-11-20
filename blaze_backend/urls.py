"""
URL configuration for blaze_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
# blaze_backend/urls.py
from django.contrib import admin
from django.urls import path, include, re_path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.conf import settings
from django.conf.urls.static import static

from RideVTC.views import RideDetailView, RideViewSet, RideVehicleViewSet, DriverRideLocationView, DriverNavEventViewSet
from rest_framework.routers import DefaultRouter
from vehicles.views import VehicleViewSet, RentalPromoView
from .health import healthz, readyz, healthz_full
from analytics.views import AnalyticsViewSet
from drivers.views import DriverDocsMeView, DriverEarningsSummary
from notifications.views import RegisterDeviceView

router = DefaultRouter()
router.register(r'vehicles', VehicleViewSet, basename='vehicles')
router.register(r'admin/analytics', AnalyticsViewSet, basename='admin-analytics')
router.register(r"ridevtc/driver-nav/events", DriverNavEventViewSet, basename="ridevtc-driver-nav-events")

# ✅ route explicite vers l’action `location` du ViewSet
ride_location = RideViewSet.as_view({'post': 'location'})

urlpatterns = [
    re_path(r"^healthz/?$", healthz, name="healthz"),
    re_path(r"^readyz/?$", readyz, name="readyz"),
    re_path(r"^healthz/full/?$", healthz_full),
    re_path(r"^api/healthz/?$", healthz, name="api-healthz"),
    re_path(r"^api/readyz/?$", readyz, name="api-readyz"),
    re_path(r"^api/healthz/full/?$", healthz_full),
    re_path(r"^api/driver/docs/?$", DriverDocsMeView.as_view()),
    re_path(r"^api/driver/earnings/summary/?$", DriverEarningsSummary.as_view()),

    path('admin/', admin.site.urls),

    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/', include(router.urls)),

    path('api/users/', include('users.urls')),
    path('api/drivers/', include('drivers.urls')),
    path('api/maps/', include('mapsproxy.urls')),
    path("api/rental/", include("vehicles.urls")),

    # ✅ la route POST qui manquait
    path('api/rides/<int:pk>/location/', ride_location, name='ride-location'),

    path('api/', include('RideVTC.urls')),
    path("api/notifications/", include("notifications.urls")),

    # (facultatif) tu peux garder aussi ta route “drivers location” si tu veux
    # path('api/drivers/rides/<int:pk>/location/', DriverRideLocationView.as_view(), name='driver-ride-location'),
]
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)