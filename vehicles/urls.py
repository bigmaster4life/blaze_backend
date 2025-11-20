from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import VehicleViewSet, RentalPromoView, RentalViewSet, RentalMobileInitiate, RentalMobileStatus, RentalProviderCallback

router = DefaultRouter()
router.register(r'vehicles', VehicleViewSet, basename='vehicle')
router.register(r'', RentalViewSet, basename='rental')  # rien après /api/rental/

urlpatterns = [
    path('', include(router.urls)),
    path("promo/", RentalPromoView.as_view(), name="rental-promo"),
    path("rentals/<int:pk>/mobile/initiate/", RentalMobileInitiate.as_view(), name="rental-mobile-initiate"),
    path("rentals/payment/status/", RentalMobileStatus.as_view(), name="rental-mobile-status"),
    path("rentals/payment/callback/", RentalProviderCallback.as_view(), name="rental-mobile-callback"),
]