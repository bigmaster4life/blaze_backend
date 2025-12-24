from django.urls import path, include
from .views import RegisterView, MeView, LoginView, UserListView, CustomerProfileViewSet, PhoneLoginView, RequestOTPView, VerifyOTPView, SetPasswordView, RequestOTPEmailView, CheckPhoneView, VerifyOTPEmailView
from rest_framework.routers import DefaultRouter


router = DefaultRouter()
router.register(r'profiles', CustomerProfileViewSet, basename='profile')


urlpatterns = [
    path('', UserListView.as_view(), name='user-root-list'),
    path('register/', RegisterView.as_view(), name='register'),
    path('me/', MeView.as_view(), name='me'),
    path('login/', LoginView.as_view(), name='login'),
    path('login-phone/', PhoneLoginView.as_view(), name='login_phone'),
    
    path('auth/request_otp/', RequestOTPView.as_view(), name='request-otp'),
    path("auth/check-phone/", CheckPhoneView.as_view(), name="auth-check-phone"),
    path('auth/verify_otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('list/', UserListView.as_view(), name='user-list'),
    path('set-password/', SetPasswordView.as_view(), name='users-set-password'),
    path("auth/request-otp-email/", RequestOTPEmailView.as_view(), name="request-otp-email"),
    path("auth/verify-otp-email/", VerifyOTPEmailView.as_view(), name="verify-otp-email"),
    

    path('', include(router.urls)),
]
