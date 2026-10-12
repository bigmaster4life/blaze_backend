# RideVTC/models.py

from django.db import models
from django.conf import settings
from drivers.models import Driver

class RideVehicle(models.Model):
    shared_enabled = models.BooleanField(default=False)
    passenger_capacity = models.PositiveSmallIntegerField(default=4)
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ride_vehicles'
    )
    brand = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    

    plate = models.CharField(max_length=32, unique=True, null=True, blank=True)
    color = models.CharField(max_length=32, null=True, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    insurance_valid_until = models.DateField(null=True, blank=True)
    technical_valid_until = models.DateField(null=True, blank=True)

    category = models.CharField(
        max_length=10,
        choices=Driver.CATEGORY_CHOICES,
        default='eco',
      )  # ex: SUV, Berline, Moto
    city = models.CharField(max_length=100)
    latitude = models.FloatField()
    longitude = models.FloatField()
    available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        email = getattr(self.driver, "email", None) or "?"
        return f"{self.brand} {self.model} ({self.driver.email})"


    
class Ride(models.Model):
    ride_mode = models.CharField(max_length=16, default="classic", choices=[("classic", "Classique"), ("shared", "Partagé")])
    passenger_count = models.PositiveSmallIntegerField(default=1)
    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('accepted', 'Acceptée'),
        ('rejected', 'Rejetée'),
        ('in_progress', 'En cours'),
        ('completed', 'Terminée'),
        ('cancelled', 'Annulée'),
    ]
    CATEGORY_CHOICES = [
        ('eco', 'Éco'),
        ('clim', 'Climatisé'),
        ('vip', 'VIP'),
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
    category = models.CharField(
        max_length=10,
        choices=CATEGORY_CHOICES,
        default='eco'
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
    final_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    pause_started_at = models.DateTimeField(null=True, blank=True)
    total_pause_seconds = models.PositiveIntegerField(default=0)
    pause_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    requested_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    driver_lat = models.FloatField(null=True, blank=True)
    driver_lng = models.FloatField(null=True, blank=True)
    customer_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    customer_comment = models.TextField(null=True, blank=True)
    rated_at = models.DateTimeField(null=True, blank=True)
    pickup_city = models.CharField(max_length=64, blank=True, null=True, default="")
    boost_amount = models.PositiveIntegerField(default=0)
    dispatch_status = models.CharField(
        max_length=24,
        default="searching",
        choices=[
            ("searching", "searching"),
            ("need_boost", "need_boost"),
            ("dispatched", "dispatched"),
            ("accepted", "accepted"),
            ("cancelled", "cancelled"),
        ],
    )
    dispatch_attempt = models.PositiveSmallIntegerField(default=0)
    max_radius_km_used = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"Course #{self.id} - {self.user.email} ➡ {self.dropoff_location}"
    
# models.py
class Payment(models.Model):
    WALLET_CHOICES = (
        ("MOBILE_MONEY", "Mobile Money"),
        ("BLAZE", "Blaze Wallet"),
        ("CASH", "Cash"),
    )
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("SUCCESS", "Success"),
        ("FAILED", "Failed"),
    )
    PROVIDER_CHOICES = (
        ("AIRTEL_MONEY", "Airtel Money"),
        ("CASH", "Cash"),
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

# --- Presence chauffeur (WS heartbeat) ---
class DriverPresence(models.Model):
    """
    Suivi de l'état en ligne/offline d'un chauffeur.
    On met à jour is_online/last_seen à la connexion WS, ping périodique, et à la déconnexion.
    """
    driver = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="presence"
    )
    city = models.CharField(max_length=50, default="", blank=True)
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"Presence(driver={self.driver_id}, online={self.is_online}, last_seen={self.last_seen})"
    

class DriverNavEvent(models.Model):
    class EventType(models.TextChoices):
        NAV_STARTED     = "nav_started"
        REROUTE         = "reroute"
        OFF_ROUTE       = "off_route"
        ETA_UPDATE      = "eta_update"
        ARRIVED_PICKUP  = "arrived_pickup"
        ARRIVED_DROPOFF = "arrived_dropoff"
        PAUSE_NAV       = "pause_nav"
        RESUME_NAV      = "resume_nav"
        ERROR           = "error"

    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ridevtc_nav_events"
    )
    request_id = models.CharField(max_length=64, db_index=True)
    event_type = models.CharField(max_length=32, choices=EventType.choices, db_index=True)

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    speed = models.FloatField(null=True, blank=True)

    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["driver", "created_at"]),
            models.Index(fields=["request_id", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.driver_id} {self.event_type} {self.request_id}"


class RidePricing(models.Model):
    """Immutable quote basis; absent on legacy rides, never backfilled implicitly."""
    ride = models.OneToOneField(Ride, on_delete=models.CASCADE, related_name='pricing')
    version = models.CharField(max_length=32)
    reference_price = models.DecimalField(max_digits=10, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    quoted_distance_km = models.FloatField()
    quoted_duration_seconds = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class PaymentAllocation(models.Model):
    """Accounting snapshot, not a bank transfer or a withdrawal balance."""
    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name='allocation')
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2)
    platform_amount = models.DecimalField(max_digits=12, decimal_places=2)
    driver_amount = models.DecimalField(max_digits=12, decimal_places=2)
    cash_commission_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class SharedTrip(models.Model):
    driver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='shared_trips')
    vehicle = models.ForeignKey(RideVehicle, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default='active')
    stops = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)
    last_lat = models.FloatField(null=True)
    last_lng = models.FloatField(null=True)
    last_location_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['driver'], condition=models.Q(state='active'), name='one_active_shared_trip_per_driver')]


class SharedBooking(models.Model):
    trip = models.ForeignKey(SharedTrip, on_delete=models.CASCADE, related_name='bookings')
    ride = models.OneToOneField(Ride, on_delete=models.CASCADE, related_name='shared_booking')
    travelled_m = models.FloatField(default=0)
    shared_m = models.FloatField(default=0)
    pickup_at = models.DateTimeField(null=True)
    dropoff_at = models.DateTimeField(null=True)
    discount_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0)
