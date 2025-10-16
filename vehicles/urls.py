from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import VehicleViewSet, RentalPromoView, RentalViewSet

router = DefaultRouter()
router.register(r'vehicles', VehicleViewSet, basename='vehicle')
router.register(r'', RentalViewSet, basename='rental')  # rien après /api/rental/

urlpatterns = [
    path('', include(router.urls)),
    path("promo/", RentalPromoView.as_view(), name="rental-promo"),
]