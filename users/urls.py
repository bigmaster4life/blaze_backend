from django.urls import path, include
from .views import RegisterView, MeView, LoginView, UserListView, CustomerProfileViewSet, PhoneLoginView, RequestOTPView, VerifyOTPView, SetPasswordView
from rest_framework.routers import DefaultRouter


router = DefaultRouter()
router.register(r'profiles', CustomerProfileViewSet, basename='profile')


urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('me/', MeView.as_view(), name='me'),
    path('login/', LoginView.as_view(), name='login'),
    path('login-phone/', PhoneLoginView.as_view(), name='login_phone'),
    
    path('auth/request_otp/', RequestOTPView.as_view(), name='request-otp'),
    path('auth/verify_otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('list/', UserListView.as_view(), name='user-list'),
    path('set-password/', SetPasswordView.as_view(), name='users-set-password'),

    path('', include(router.urls)),
]
