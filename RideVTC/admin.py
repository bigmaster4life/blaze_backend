from django.contrib import admin
from .models import DriverNavEvent

@admin.register(DriverNavEvent)
class DriverNavEventAdmin(admin.ModelAdmin):
    list_display = ("id","driver","request_id","event_type","created_at")
    list_filter = ("event_type","created_at")
    search_fields = ("request_id","driver__email","driver__id")