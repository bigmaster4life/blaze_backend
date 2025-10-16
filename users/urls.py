from django.urls import path, include
from .views import RegisterView, MeView, LoginView, UserListView, CustomerProfileViewSet, PhoneLoginView
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'profiles', CustomerProfileViewSet, basename='profile')


urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('me/', MeView.as_view(), name='me'),
    path('login/', LoginView.as_view(), name='login'),
    path('login-phone/', PhoneLoginView.as_view(), name='login_phone'),
    path('', UserListView.as_view(), name='user-list'),

    path('', include(router.urls)),
]