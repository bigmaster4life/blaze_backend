# RideVTC/models.py

from django.db import models
from django.conf import settings

class RideVehicle(models.Model):
    driver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ride_vehicles')
    brand = models.CharField(max_length=100)
    model = models.CharField(max_length=100)

    plate = models.CharField(max_length=32, unique=True, null=True, blank=True)
    color = models.CharField(max_length=32, null=True, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    insurance_valid_until = models.DateField(null=True, blank=True)
    technical_valid_until = models.DateField(null=True, blank=True)

    category = models.CharField(max_length=50)  # ex: SUV, Berline, Moto
    city = models.CharField(max_length=100)
    latitude = models.FloatField()
    longitude = models.FloatField()
    available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.brand} {self.model} ({self.driver.email})"


    
class Ride(models.Model):
    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('accepted', 'Acceptée'),
        ('rejected', 'Rejetée'),
        ('in_progress', 'En cours'),
        ('completed', 'Terminée'),
        ('cancelled', 'Annulée'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='rides'
    )
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='driven_rides'
    )
    vehicle = models.ForeignKey(
        'RideVehicle',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rides'
    )
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)
    pickup_lat = models.FloatField(null=True, blank=True)
    pickup_lng = models.FloatField(null=True, blank=True)
    dropoff_lat = models.FloatField(null=True, blank=True)
    dropoff_lng = models.FloatField(null=True, blank=True)
    distance_km = models.FloatField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    requested_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    driver_lat = models.FloatField(null=True, blank=True)
    driver_lng = models.FloatField(null=True, blank=True)
    customer_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    customer_comment = models.TextField(null=True, blank=True)
    rated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Course #{self.id} - {self.user.email} ➡ {self.dropoff_location}"
    
# models.py
class Payment(models.Model):
    WALLET_CHOICES = (
        ("MOBILE_MONEY", "Mobile Money"),
        ("BLAZE", "Blaze Wallet"),
    )
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("SUCCESS", "Success"),
        ("FAILED", "Failed"),
    )
    PROVIDER_CHOICES = (
        ("AIRTEL_MONEY", "Airtel Money"),
        # plus tard: ("MOOV_MONEY", "Moov Money"),
    )

    ride = models.ForeignKey(Ride, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="XAF")
    wallet = models.CharField(max_length=16, choices=WALLET_CHOICES)
    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES, default="AIRTEL_MONEY")

    msisdn = models.CharField(max_length=32, blank=True, null=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    provider_txid = models.CharField(max_length=128, blank=True, null=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="PENDING")
    meta = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["idempotency_key"]),
            models.Index(fields=["provider_txid"]),
        ]

    def __str__(self):
        return f"Payment#{self.pk} {self.wallet}/{self.provider} {self.amount} {self.currency} {self.status}"
    

class DriverStats(models.Model):
    """Agrégats de notation pour chaque chauffeur (User)."""
    driver = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='driver_stats'
    )
    rating_avg = models.FloatField(default=0.0)
    rating_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Stats {self.driver_id} avg={self.rating_avg:.2f} n={self.rating_count}"
    
class DriverRating(models.Model):
    """Une note par course, client -> chauffeur (1..5)."""
    ride = models.OneToOneField('Ride', on_delete=models.CASCADE, related_name='driver_rating')
    driver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ratings_received')
    passenger = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ratings_given')
    rating = models.PositiveSmallIntegerField()  # 1..5
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
