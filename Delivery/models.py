# Delivery/models.py
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone


class CityChoices(models.TextChoices):
    LIBREVILLE = "libreville", "Libreville"
    OWENDO = "owendo", "Owendo"
    ANGONDJE = "angondje", "Angondjé"
    NTOUM = "ntoum", "Ntoum"
    KANGO = "kango", "Kango"
    PORT_GENTIL = "port_gentil", "Port-Gentil"
    FRANCEVILLE = "franceville", "Franceville"


class DeliveryDriver(models.Model):
    VEHICLE_MOTO = "moto"
    VEHICLE_CAR = "car"
    VEHICLE_VAN = "van"
    VEHICLE_CHOICES = (
        (VEHICLE_MOTO, "Moto"),
        (VEHICLE_CAR, "Voiture"),
        (VEHICLE_VAN, "Van"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="delivery_driver_profile",
        null=True,
        blank=True,
    )
    fcm_token = models.CharField(max_length=255, blank=True, null=True)

    # Infos de base
    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30)

    city = models.CharField(
        max_length=20,
        choices=CityChoices.choices,
        default=CityChoices.LIBREVILLE
    )

    vehicle_type = models.CharField(
        max_length=10,
        choices=VEHICLE_CHOICES,
        default=VEHICLE_MOTO
    )

    # Onboarding & validation manuelle
    onboarding_completed = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    # Documents
    identity_card = models.FileField(
        upload_to="delivery_drivers/identity_cards/",
        null=True,
        blank=True
    )
    driving_license = models.FileField(
        upload_to="delivery_drivers/licenses/",
        null=True,
        blank=True
    )
    photo = models.ImageField(
        upload_to="delivery_drivers/photos/",
        null=True,
        blank=True
    )

    # Disponibilité
    is_active = models.BooleanField(default=True)
    is_available = models.BooleanField(default=True)

    # Localisation
    last_lat = models.FloatField(null=True, blank=True)
    last_lng = models.FloatField(null=True, blank=True)
    last_location_update = models.DateTimeField(null=True, blank=True)

    # Stats
    total_deliveries = models.PositiveIntegerField(default=0)
    total_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    is_blocked = models.BooleanField(default=False)
    block_reason = models.TextField(blank=True, null=True)

    def mark_verified(self):
        self.is_verified = True
        self.verified_at = timezone.now()
        self.save(update_fields=["is_verified", "verified_at"])

    def __str__(self):
        return f"{self.full_name} ({self.vehicle_type})"


class DeliveryZone(models.Model):
    # (tu peux la garder pour plus tard; v1 pricing = city enum)
    name = models.CharField(max_length=80, unique=True)
    base_price = models.PositiveIntegerField()
    keywords = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)


class DeliveryRequest(models.Model):
    TYPE_PACKAGE = "package"
    TYPE_FOOD = "food"
    TYPE_DOCUMENTS = "documents"
    TYPE_EXPRESS = "express"
    TYPE_CHOICES = (
        (TYPE_PACKAGE, "Colis & Paquets"),
        (TYPE_FOOD, "Nourriture & Courses"),
        (TYPE_DOCUMENTS, "Documents"),
        (TYPE_EXPRESS, "Express"),
    )

    STATUS_PENDING = "pending"
    STATUS_SEARCHING = "searching"
    STATUS_OPERATOR_REQUIRED = "operator_required"  # ✅ VAN
    STATUS_ASSIGNED = "assigned"
    STATUS_PICKED_UP = "picked_up"
    STATUS_DELIVERED = "delivered"
    STATUS_CANCELLED = "cancelled"
    STATUS_FAILED = "failed"
    STATUS_NO_DRIVER = "no_driver"
    STATUS_DELIVERED_BY_DRIVER = "delivered_by_driver"
    STATUS_CONFIRMED_BY_CLIENT = "confirmed_by_client"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SEARCHING, "Searching"),
        (STATUS_OPERATOR_REQUIRED, "Operator required"),
        (STATUS_ASSIGNED, "Assigned"),
        (STATUS_PICKED_UP, "Picked up"),
        (STATUS_DELIVERED_BY_DRIVER, "Delivered by driver"),
        (STATUS_CONFIRMED_BY_CLIENT, "Confirmed by client"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_FAILED, "Failed"),
        (STATUS_NO_DRIVER, "No driver available"),
    )

    PAY_CASH = "cash"
    PAYMENT_CHOICES = ((PAY_CASH, "Cash"),)

    VAN_SERVICE_MOVING = "moving"
    VAN_SERVICE_SHIPPING = "shipping"
    VAN_SERVICE_CHOICES = (
        (VAN_SERVICE_MOVING, "Déménagement"),
        (VAN_SERVICE_SHIPPING, "Shipping"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="delivery_requests")

    type = models.CharField(max_length=12, choices=TYPE_CHOICES, default=TYPE_PACKAGE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    # ✅ ville choisie (pour pricing + filtrage drivers)
    city = models.CharField(max_length=20, choices=CityChoices.choices, default=CityChoices.LIBREVILLE)

    # (tu peux garder zone si déjà en prod)
    zone = models.ForeignKey(DeliveryZone, on_delete=models.SET_NULL, null=True, blank=True)

    pickup_zone = models.CharField(
        max_length=20,
        choices=CityChoices.choices,
        default=CityChoices.LIBREVILLE,
    )

    dropoff_zone = models.CharField(
        max_length=20,
        choices=CityChoices.choices,
        default=CityChoices.LIBREVILLE,
    )

    pickup_label = models.CharField(max_length=200)
    pickup_lat = models.FloatField(null=True, blank=True)
    pickup_lng = models.FloatField(null=True, blank=True)

    dropoff_label = models.CharField(max_length=200)
    dropoff_lat = models.FloatField(null=True, blank=True)
    dropoff_lng = models.FloatField(null=True, blank=True)

    contact_name = models.CharField(max_length=120)
    contact_phone = models.CharField(max_length=30)
    note = models.TextField(blank=True, default="")
    order_details = models.JSONField(default=dict, blank=True)

    vehicle_type = models.CharField(
        max_length=10,
        choices=DeliveryDriver.VEHICLE_CHOICES,
        default=DeliveryDriver.VEHICLE_MOTO,
    )

    # ✅ VAN (form simplifié)
    van_service = models.CharField(max_length=20, choices=VAN_SERVICE_CHOICES, null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    estimated_price = models.PositiveIntegerField(default=0)
    final_price = models.PositiveIntegerField(null=True, blank=True)

    distance_km = models.FloatField(default=0)

    payment_method = models.CharField(max_length=10, choices=PAYMENT_CHOICES, default=PAY_CASH)
    is_paid = models.BooleanField(default=False)  # delivery fee only; goods are separate
    payment_payer = models.CharField(max_length=12, choices=[('sender', 'Expéditeur au retrait'), ('recipient', 'Destinataire à la remise'), ('unknown', 'Payeur non renseigné')], default='sender')
    cash_received_at = models.DateTimeField(null=True, blank=True)
    purchase_funds_amount = models.PositiveIntegerField(null=True, blank=True)
    purchase_funds_received_at = models.DateTimeField(null=True, blank=True)
    purchase_actual_amount = models.PositiveIntegerField(null=True, blank=True)
    purchase_advance_accepted_at = models.DateTimeField(null=True, blank=True)
    purchase_reimbursed_at = models.DateTimeField(null=True, blank=True)
    purchase_change_returned_at = models.DateTimeField(null=True, blank=True)

    assigned_driver = models.ForeignKey(
        DeliveryDriver,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_deliveries",
    )
    assigned_by_operator = models.BooleanField(default=False)

    operator_note = models.TextField(blank=True, default="")
    operator_handled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def compute_estimated_price(self) -> int:
        # VAN = devis opérateur
        if self.vehicle_type == DeliveryDriver.VEHICLE_VAN:
            return 0

        # Express (livreur dédié)
        if self.type == self.TYPE_EXPRESS:
            return 5000
        
        pickup_zone = self.pickup_zone or CityChoices.LIBREVILLE
        dropoff_zone = self.dropoff_zone or CityChoices.LIBREVILLE

        # distance calculée côté frontend/backend si dispo
        distance_km = 0
        try:
            if hasattr(self, "distance_km") and self.distance_km:
                distance_km = float(self.distance_km)
        except Exception:
            distance_km = 0
        
        urban_zones = {
            CityChoices.LIBREVILLE,
            CityChoices.OWENDO,
            CityChoices.ANGONDJE,
        }

        outer_zones = {
            CityChoices.NTOUM,
            CityChoices.KANGO,
        }

        # 1) Même zone
        if pickup_zone == dropoff_zone:
            if distance_km <= 3:
                return 1500
            return 2000
        
        # 2) Zones urbaines
        if pickup_zone in urban_zones and dropoff_zone in urban_zones:
            if distance_km <= 3:
                return 2000
            return 3000
        
        # 3) Zones extérieures
        if pickup_zone in outer_zones or dropoff_zone in outer_zones:
            if distance_km <= 15:
                return 5000
            elif distance_km <= 30:
                return 7000
            elif distance_km <= 50:
                return 10000
            return 15000

        # Port-Gentil / Franceville = 2500
        special_zones = {
            CityChoices.PORT_GENTIL,
            CityChoices.FRANCEVILLE,
        }
        if pickup_zone in special_zones or dropoff_zone in special_zones:
            return 2500

        return 2000

    def save(self, *args, **kwargs):
        if not self.estimated_price:
            self.estimated_price = self.compute_estimated_price()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"DeliveryRequest #{self.id} ({self.type}) - {self.status}"


class DeliveryOffer(models.Model):
    """
    Offre envoyée à un livreur. Le livreur doit accepter.
    Timeout global: expires_at (120s).
    """
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_EXPIRED = "expired"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_EXPIRED, "Expired"),
    )

    request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, related_name="offers")
    driver = models.ForeignKey(DeliveryDriver, on_delete=models.CASCADE, related_name="offers")

    distance_km = models.FloatField(default=0)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)

    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("request", "driver")

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class DeliveryPaymentAllocation(models.Model):
    """Fee split recorded at collection; not a wallet or an executed payout."""
    request = models.OneToOneField(DeliveryRequest, on_delete=models.PROTECT, related_name='payment_allocation')
    driver = models.ForeignKey(DeliveryDriver, on_delete=models.PROTECT, related_name='payment_allocations')
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2)
    platform_amount = models.DecimalField(max_digits=12, decimal_places=2)
    driver_amount = models.DecimalField(max_digits=12, decimal_places=2)
    cash_commission_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
