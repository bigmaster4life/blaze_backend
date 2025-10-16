# users/models.py
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.conf import settings

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, first_name=None, last_name=None, user_type=None, **extra_fields):
        if not email:
            raise ValueError("L'adresse email est obligatoire")
        if not first_name:
            raise ValueError("Le prénom est obligatoire")
        if not last_name:
            raise ValueError("Le nom est obligatoire")
        if not user_type:
            raise ValueError("Le type d'utilisateur est obligatoire")

        email = self.normalize_email(email)

        # ❌ NE PAS envoyer full_name (c’est une @property, pas un champ)
        extra_fields.pop("full_name", None)

        user = self.model(
            email=email,
            first_name=first_name,
            last_name=last_name,
            user_type=user_type,
            **extra_fields
        )
        user.set_password(password)
        # bonne pratique : préciser la DB manager
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        # valeurs par défaut pour pouvoir créer sans prompts supplémentaires
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("first_name", "Admin")
        extra_fields.setdefault("last_name", "User")
        extra_fields.setdefault("user_type", "manager_staff")

        # idem : ne jamais passer full_name
        extra_fields.pop("full_name", None)

        return self.create_user(
            email=email,
            password=password,
            **extra_fields
        )

class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    first_name = models.CharField(max_length=100, blank=True, default='')
    last_name = models.CharField(max_length=100, blank=True, default='')
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    user_type = models.CharField(
        max_length=20,
        choices=[
            ("client", "Client"),
            ("chauffeur", "Chauffeur"),
            ("loueur", "Loueur"),
            ("manager_staff", "Manager Staff"),
            ("employee_staff", "Employé Staff"),
        ],
        default="client"
    )
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'user_type']

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})".strip()

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return (self.first_name or self.email).strip()

class CustomerProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=[('Homme', 'Homme'), ('Femme', 'Femme')], blank=True)
    email = models.EmailField(null=True, blank=True)
    photo = models.ImageField(upload_to='profile_photos/', null=True, blank=True)
    profile_completed = models.BooleanField(default=False)

    def __str__(self):
        # ✅ le champ s’appelle phone_number, pas phone
        phone = self.user.phone_number or ''
        return f"Profil de {phone}".strip()