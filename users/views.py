from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from .serializers import (
    RegisterSerializer, UserSerializer, CustomerProfileSerializer, PhoneLoginSerializer
)
from rest_framework import status, viewsets, permissions
from .models import CustomUser, CustomerProfile
from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.generics import ListAPIView
from django.shortcuts import get_object_or_404
from rest_framework.parsers import MultiPartParser, FormParser

class IsOwnerProfile(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user

class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            return Response({"message": "Utilisateur créé avec succès"}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

class LoginView(APIView):
    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        user = authenticate(request, email=email, password=password)
        if user is not None:
            refresh = RefreshToken.for_user(user)

            return Response({
                'token': str(refresh.access_token),
                'refresh': str(refresh),
                'user_type': user.user_type,
                'email': user.email,
                'full_name': user.full_name,
            })
        else:
            return Response({'error': 'Identifiants invalides'}, status=status.HTTP_401_UNAUTHORIZED)

class UserListView(ListAPIView):
    queryset = CustomUser.objects.all()
    serializer_class = UserSerializer
    permission_classes = [AllowAny]


class CustomerProfileViewSet(viewsets.ModelViewSet):
  queryset = CustomerProfile.objects.all()
  serializer_class = CustomerProfileSerializer
  permission_classes = [permissions.IsAuthenticated]
  parser_classes = [MultiPartParser, FormParser]  # pour upload photo

  def get_queryset(self):
    # limite aux profils du user courant
    return CustomerProfile.objects.filter(user=self.request.user)

  def perform_create(self, serializer):
    serializer.save(user=self.request.user)

  @action(methods=["get", "put", "patch"], detail=False, url_path="me")
  def me(self, request):
    """
    GET    /api/profiles/me/     -> lire mon profil (404 si pas encore créé)
    PUT    /api/profiles/me/     -> remplacer (creation implicite si inexistant)
    PATCH  /api/profiles/me/     -> mise à jour partielle
    """
    try:
      profile = CustomerProfile.objects.get(user=request.user)
    except CustomerProfile.DoesNotExist:
      if request.method == "GET":
        return Response({"detail": "Profile not found."}, status=404)
      # création si on fait PUT/PATCH sans profil existant
      serializer = self.get_serializer(data=request.data)
      serializer.is_valid(raise_exception=True)
      serializer.save(user=request.user)
      return Response(serializer.data, status=201)

    partial = request.method == "PATCH"
    serializer = self.get_serializer(profile, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)
  
class PhoneLoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        print("DEBUG /login-phone payload ->", request.data)
        ser = PhoneLoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.save()  # retourne tokens + user
        return Response(data, status=status.HTTP_200_OK)