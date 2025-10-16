from django.db import models
from django.conf import settings
from django.utils import timezone
from decimal import Decimal, ROUND_UP
import secrets
import string

class Vehicle(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='vehicles',
        null=True,
        blank=True
    )
    owner_phone = models.CharField(max_length=32, blank=True)
    brand = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    transmission = models.CharField(max_length=50, choices=[
        ('manual', 'Manuelle'),
        ('automatic', 'Automatique')
    ])
    fuel_type = models.CharField(max_length=50, choices=[
        ('essence', 'Essence'),
        ('diesel', 'Diesel'),
        ('hybrid', 'Hybride'),
        ('electric', 'Électrique')
    ])
    seats = models.IntegerField()
    registration_number = models.CharField(max_length=50, unique=True)
    daily_price = models.DecimalField(max_digits=10, decimal_places=2)
    city = models.CharField(max_length=100)
    category = models.CharField(max_length=100)
    is_available = models.BooleanField(default=True)
    image = models.ImageField(upload_to='vehicle_images/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    is_validated = models.BooleanField(default=False)
    description = models.TextField(blank=True)

    def __str__(self):
        return f"{self.brand} {self.model} ({self.registration_number})"
    
class PromoQuerySet(models.QuerySet):
    def active(self, now=None):
        now = now or timezone.now()
        return self.filter(
            is_active=True
        ).filter(
            models.Q(starts_at__isnull=True) | models.Q(starts_at__lte=now)
        ).filter(
            models.Q(ends_at__isnull=True) | models.Q(ends_at__gte=now)
        )

    def for_city(self, city: str | None):
        if not city:
            # on préfère les promos globales (city NULL) par priorité
            return self.filter(models.Q(city__isnull=True) | models.Q(city=""))
        return self.filter(models.Q(city__iexact=city) | models.Q(city__isnull=True) | models.Q(city=""))

class Promo(models.Model):
    title = models.CharField(max_length=120)
    subtitle = models.CharField(max_length=200, blank=True)
    cta = models.CharField("CTA label", max_length=60, blank=True)
    url = models.URLField("CTA URL", blank=True)
    badge = models.CharField(max_length=24, blank=True)  # ex: "-15%"
    image = models.ImageField(upload_to="rental_promos/", blank=True)  # optionnel

    city = models.CharField(
        max_length=80, blank=True, null=True,
        help_text="Si rempli, promo ciblée sur cette ville (insensible à la casse). Laisser vide = globale."
    )

    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(blank=True, null=True)
    ends_at = models.DateTimeField(blank=True, null=True)
    priority = models.IntegerField(default=0, help_text="Plus grand = plus prioritaire")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PromoQuerySet.as_manager()

    class Meta:
        ordering = ["-priority", "-updated_at", "-id"]

    def __str__(self):
        city = self.city or "GLOBAL"
        return f"[{city}] {self.title} (prio {self.priority})"
    
class Rental(models.Model):
    STATUS_CHOICES = [
        ('pending', 'En attente'),       # option (hold) posée
        ('confirmed', 'Confirmée'),      # payé mais pas encore remis
        ('in_progress', 'En cours'),     # véhicule remis
        ('finished', 'Terminée'),        # rendu
        ('canceled', 'Annulée'),
        ('expired', 'Expirée'),          # no-show (hold expiré)
    ]
    PAY_CHOICES = [
        ('cash', 'Cash'),
        ('wallet', 'Wallet'),
        ('mobile', 'Mobile Money'),
    ]

    vehicle = models.ForeignKey('vehicles.Vehicle', on_delete=models.CASCADE, related_name='rentals')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='rentals')
    identification_code = models.CharField(max_length=12, blank=True)

    start_date = models.DateTimeField()
    end_date   = models.DateTimeField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    payment_method = models.CharField(max_length=20, choices=PAY_CHOICES, blank=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # pour les réservations en "pending" (option/hold)
    hold_expires_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vehicle', 'status']),
            models.Index(fields=['start_date', 'end_date']),
        ]

    def __str__(self):
        return f"Rental #{self.pk} - {self.vehicle} [{self.status}]"
    
    def _night_count(self) -> int:
        """Nombre de jours facturés (arrondi au supérieur)."""
        seconds = (self.end_date - self.start_date).total_seconds()
        days = Decimal(seconds) / Decimal(86400)
        return int(days.to_integral_value(rounding=ROUND_UP))
    def recompute_total(self):
        """Calcule total_amount en fonction du daily_price du véhicule."""
        price = Decimal(self.vehicle.daily_price or 0)
        self.total_amount = price * Decimal(self._night_count())

def generate_ident_code(length=6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def set_vehicle_availability(vehicle: 'Vehicle'):
    """
    Met Vehicle.is_available à False s'il existe une location active
    (pending non expiré, confirmed, in_progress).
    """
    now = timezone.now()
    has_active = vehicle.rentals.filter(
        status__in=['pending', 'confirmed', 'in_progress']
    ).exclude(
        # exclut les "pending" qui ont expiré
        models.Q(status='pending') & models.Q(hold_expires_at__lt=now)
    ).exists()

    new_value = not has_active
    if vehicle.is_available != new_value:
        vehicle.is_available = new_value
        vehicle.save(update_fields=['is_available'])

class VehicleBooking(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='bookings')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=20, default='CONFIRMED')  # CONFIRMED / CANCELED ...

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['vehicle', 'start_at', 'end_at']),
        ]