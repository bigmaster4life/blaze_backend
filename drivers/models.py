# drivers/models.py
from django.db import models
from django.conf import settings
from django.utils import timezone


def driver_upload_path(instance, filename):
    """
    Stocke les fichiers sous drivers/<user_id>/<filename>.
    Si le driver est orphelin (user=None), on place dans drivers/orphan/<filename>.
    """
    uid = getattr(instance.user, "id", None) or "orphan"
    return f"drivers/{uid}/{filename}"


class Driver(models.Model):
    CATEGORY_CHOICES = [
        ('eco', 'Éco'),
        ('clim', 'Climatisé'),
        ('vip', 'VIP'),
    ]
    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('approved', 'Approuvé'),
        ('rejected', 'Rejeté'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    full_name = models.CharField(max_length=100)
    email = models.EmailField(default='default_driver@example.com')
    phone = models.CharField(max_length=20)
    vehicle_plate = models.CharField(max_length=20)
    role = models.CharField(max_length=50, default='chauffeur')

    # Laisse null/blank pour survivre aux anciennes lignes orphelines.
    # Tu pourras repasser à null=False une fois les données nettoyées.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='driver_profile',
        null=True,
        blank=True,
    )

    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default='eco')

    license_file = models.FileField(upload_to=driver_upload_path, null=True, blank=True)
    id_card_file = models.FileField(upload_to=driver_upload_path, null=True, blank=True)
    insurance_file = models.FileField(upload_to=driver_upload_path, null=True, blank=True)

    accepted_terms = models.BooleanField(default=False)

    must_reset_password = models.BooleanField(default=True)
    onboarding_completed = models.BooleanField(default=False)

    is_blocked = models.BooleanField(default=False)
    block_reason = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(default=timezone.now)

    is_online = models.BooleanField(default=False)
    last_latitude = models.FloatField(null=True, blank=True)
    last_longitude = models.FloatField(null=True, blank=True)

    def __str__(self):
        """
        Représentation sûre pour l’admin/logs :
        - préfère un nom lié à l’utilisateur si présent
        - sinon fallback sur full_name, puis téléphone, puis ID
        """
        user_part = None
        if self.user_id:
            # Essaie d’abord un champ 'full_name' sur CustomUser
            user_part = getattr(self.user, 'full_name', None)
            # Sinon combine first/last si disponibles
            if not user_part:
                first = getattr(self.user, 'first_name', '') or ''
                last = getattr(self.user, 'last_name', '') or ''
                combined = f"{first} {last}".strip()
                user_part = combined or getattr(self.user, 'username', None)

        display = user_part or self.full_name or self.phone or f"Driver {self.pk}"
        return f"Driver #{self.pk} – {display}"
    
def upload_to_driver(instance, filename):
    return f"drivers/{instance.user_id}/{filename}"

DOC_STATUS = (
    ('missing', 'Manquant'),
    ('pending', 'En cours'),
    ('approved', 'Validé'),
    ('rejected', 'Rejeté'),
)

class DriverDocs(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='driver_docs')

    license_file = models.FileField(upload_to=upload_to_driver, null=True, blank=True)
    insurance_file = models.FileField(upload_to=upload_to_driver, null=True, blank=True)
    registration_file = models.FileField(upload_to=upload_to_driver, null=True, blank=True)
    id_card_file = models.FileField(upload_to=upload_to_driver, null=True, blank=True)

    # statut global ou par doc (ici on met un global + note)
    status = models.CharField(max_length=16, choices=DOC_STATUS, default='pending')
    note = models.TextField(blank=True, default='')

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DriverDocs user={self.user_id} status={self.status}"