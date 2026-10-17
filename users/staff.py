"""Internal account management, separate from public customer registration."""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import generics, permissions, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

User = get_user_model()
TEAM_ROLES = ('manager_staff', 'employee_staff')


def can_read_users(user):
    return bool(user and user.is_authenticated and (user.is_superuser or (user.is_staff and user.user_type in TEAM_ROLES)))


class CanReadUsers(permissions.BasePermission):
    def has_permission(self, request, view):
        return can_read_users(request.user)


class CanCreateTeam(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_superuser or (user.is_staff and user.user_type == 'manager_staff')))


class StaffUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False, min_length=8, max_length=128)
    user_type = serializers.ChoiceField(choices=TEAM_ROLES)

    class Meta:
        model = User
        fields = ('id', 'first_name', 'last_name', 'email', 'user_type', 'password', 'is_active', 'date_joined')
        read_only_fields = ('id', 'is_active', 'date_joined')
        extra_kwargs = {'first_name': {'required': True, 'allow_blank': False}, 'last_name': {'required': True, 'allow_blank': False}}

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('Cette adresse email est déjà utilisée.')
        return value

    def validate(self, attrs):
        actor = self.context['request'].user
        if attrs['user_type'] == 'manager_staff' and not actor.is_superuser:
            raise serializers.ValidationError({'user_type': 'Seul un administrateur peut créer un gestionnaire.'})
        try:
            validate_password(attrs['password'], User(**{key: value for key, value in attrs.items() if key != 'password'}))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)}) from exc
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return User.objects.create_user(**validated_data, is_staff=True, is_superuser=False, email_verified=False)
        except IntegrityError as exc:
            raise ValidationError({'email': 'Un compte avec cette adresse existe déjà.'}) from exc


class StaffUserCreateView(generics.CreateAPIView):
    permission_classes = (CanCreateTeam,)
    serializer_class = StaffUserSerializer


class UserDirectoryPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def user_directory(params):
    queryset = User.objects.only('id', 'first_name', 'last_name', 'email', 'user_type', 'date_joined', 'is_active', 'is_staff', 'is_superuser').order_by('-date_joined', '-pk')
    if params.get('scope') == 'team':
        queryset = queryset.filter(user_type__in=TEAM_ROLES)
    search = params.get('search', '').strip()[:120]
    if search:
        queryset = queryset.filter(Q(email__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search))
    return queryset
