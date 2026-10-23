# Delivery/urls.py
from rest_framework.routers import DefaultRouter
from django.urls import path, include
from .views import DeliveryDriverLoginView, DeliveryDriverMeStatusView
from .views import (
    DeliveryRequestViewSet,
    DeliveryDriverViewSet,
    DeliveryOfferViewSet,
    DeliveryDriverLocationViewSet,
    OperatorDeliveryQueueViewSet,
    DeliveryDriverFCMTokenView,
    DeliveryDriverCreateView,
    DeliveryDriverAdminViewSet,
    DeliveryDirectionsView,
)

from .restaurant_api import RestaurantCatalog, RestaurantAdmin, MenuAdmin, OperatorOrders
from .receipts import ReceiptView, ReceiptList
router = DefaultRouter()
router.register(r'delivery/restaurants',RestaurantCatalog,basename='restaurants')
router.register(r'delivery/admin/restaurants',RestaurantAdmin,basename='admin-restaurants')
router.register(r'delivery/admin/menu',MenuAdmin,basename='admin-menu')
router.register(r'delivery/operator/restaurant-orders',OperatorOrders,basename='restaurant-orders')

# ============================================================
# CLIENT
# ============================================================
router.register(
    r"delivery/requests",
    DeliveryRequestViewSet,
    basename="delivery-requests"
)

# ============================================================
# LIVREUR
# ============================================================
router.register(
    r"delivery/drivers/me",
    DeliveryDriverViewSet,
    basename="delivery-driver-me"
)

router.register(
    r"delivery/drivers/offers",
    DeliveryOfferViewSet,
    basename="delivery-driver-offers"
)

router.register(
    r"delivery/drivers/location",
    DeliveryDriverLocationViewSet,
    basename="delivery-driver-location"
)

# ============================================================
# OPÉRATEUR (Blaze Web)
# ============================================================
router.register(
    r"delivery/operator/queue",
    OperatorDeliveryQueueViewSet,
    basename="delivery-operator-queue"
)
router.register(
    r"delivery/admin/drivers",
    DeliveryDriverAdminViewSet,
    basename="delivery-admin-drivers"
)

urlpatterns = [
    path('receipts/',ReceiptList.as_view()),
    path('receipts/<str:service>/<int:pk>/',ReceiptView.as_view()),
    path("", include(router.urls)),
    # ✅ AUTH LIVREUR
    path("delivery/drivers/login/", DeliveryDriverLoginView.as_view(), name="delivery-driver-login"),
    path("delivery/drivers/me/status/", DeliveryDriverMeStatusView.as_view(), name="delivery-driver-me-status"),
    path("driver/set-fcm-token/", DeliveryDriverFCMTokenView.as_view()),
    path("delivery/drivers/create/", DeliveryDriverCreateView.as_view(), name="delivery-driver-create"),
    path("delivery/directions/", DeliveryDirectionsView.as_view(), name="delivery-directions"),
]