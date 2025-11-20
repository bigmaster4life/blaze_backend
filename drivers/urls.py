# drivers/urls.py
from django.urls import path
from drivers.views import DriverListCreateView, DriverDetailView, InviteDriverView, DriverStatusView, CompleteOnboardingView, DriverPresenceView, MockRideRequestView, resend_invite, DriverLoginView, DriverDocsMeView, DriverEarningsSummary, DriverChangePasswordView

urlpatterns = [
    path('', DriverListCreateView.as_view(), name='driver-list'),
    path('<int:pk>/', DriverDetailView.as_view(), name='driver-detail'),
    path('invite/', InviteDriverView.as_view(), name='driver-invite'),
    path('me/change-password/', DriverChangePasswordView.as_view(), name='driver-change-password'),
    path('me/status/', DriverStatusView.as_view(), name='driver-status'),
    path("me/onboarding/", CompleteOnboardingView.as_view(), name="driver-onboarding"),
    path('me/presence/', DriverPresenceView.as_view(), name='driver-presence'),
    path('mock-request/', MockRideRequestView.as_view(), name='driver-mock-request'),
    path('<int:pk>/resend-invite/', resend_invite, name='driver-resend-invite'),
    path('login/', DriverLoginView.as_view(), name='driver-login'),
    path('docs/', DriverDocsMeView.as_view(), name='driver-docs-me'),
    path('earnings/summary/', DriverEarningsSummary.as_view(), name='driver-earnings-summary'),
]