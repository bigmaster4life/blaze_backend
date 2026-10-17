from django.contrib import admin
from .models import DeliveryDriver, DeliveryZone, DeliveryRequest, DeliveryPaymentAllocation


@admin.register(DeliveryDriver)
class DeliveryDriverAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "full_name",
        "phone",
        "city",
        "vehicle_type",
        "is_active",
        "is_available",
        "is_verified",
        "onboarding_completed",
        "updated_at",
    )

    search_fields = (
        "full_name",
        "phone",
        "user__email",
    )

    list_filter = (
        "city",
        "vehicle_type",
        "is_active",
        "is_available",
        "is_verified",
        "onboarding_completed",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
        "verified_at",
        "last_location_update",
        "total_deliveries",
        "total_earnings",
    )

    fieldsets = (
        ("Compte", {
            "fields": ("user",)
        }),
        ("Informations", {
            "fields": ("full_name", "phone", "city", "vehicle_type")
        }),
        ("Documents", {
            "fields": ("identity_card", "driving_license", "photo")
        }),
        ("Onboarding & validation", {
            "fields": ("onboarding_completed", "is_verified", "verified_at")
        }),
        ("Disponibilité", {
            "fields": ("is_active", "is_available")
        }),
        ("Localisation", {
            "fields": ("last_lat", "last_lng", "last_location_update")
        }),
        ("Statistiques", {
            "fields": ("total_deliveries", "total_earnings")
        }),
        ("Dates", {
            "fields": ("created_at", "updated_at")
        }),
    )

    actions = ["mark_as_verified"]

    @admin.action(description="✅ Valider le livreur sélectionné")
    def mark_as_verified(self, request, queryset):
        for driver in queryset:
            driver.mark_verified()


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "base_price", "is_active")
    search_fields = ("name", "keywords")
    list_filter = ("is_active",)


@admin.register(DeliveryRequest)
class DeliveryRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "type", "status", "vehicle_type", "estimated_price", "assigned_driver", "created_at")
    search_fields = ("pickup_label", "dropoff_label", "contact_name", "contact_phone")
    list_filter = ("type", "status", "vehicle_type", "payment_method", "is_paid")


@admin.register(DeliveryPaymentAllocation)
class DeliveryPaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ('id', 'request', 'driver', 'paid_amount', 'platform_amount', 'driver_amount', 'cash_commission_due', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('driver__full_name', 'driver__phone')

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
