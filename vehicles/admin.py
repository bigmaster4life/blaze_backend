# vehicles/admin.py
from django.contrib import admin
from .models import Promo, Vehicle, Rental

@admin.register(Promo)
class PromoAdmin(admin.ModelAdmin):
    list_display = ("title", "city", "is_active", "priority", "starts_at", "ends_at", "updated_at")
    list_filter = ("is_active", "city")
    search_fields = ("title", "subtitle", "city")
    ordering = ("-priority", "-updated_at")

@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ('brand','model','registration_number','city','is_available','created_at')
    list_filter  = ('city','category','is_available')
    search_fields = ('brand','model','registration_number')


@admin.register(Rental)
class RentalAdmin(admin.ModelAdmin):
    list_display = ('id','vehicle','user','status','start_date','end_date','hold_expires_at','payment_method')
    list_filter  = ('status','payment_method')
    search_fields = ('vehicle__registration_number','user__username')