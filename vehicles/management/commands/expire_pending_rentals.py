# vehicles/management/commands/expire_pending_rentals.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from vehicles.models import Rental, set_vehicle_availability

class Command(BaseCommand):
    help = "Expire les réservations en pending dont le hold a expiré"

    def handle(self, *args, **kwargs):
        now = timezone.now()
        qs = Rental.objects.filter(status='pending', hold_expires_at__lt=now)
        count = 0
        for r in qs:
            r.status = 'expired'
            r.save(update_fields=['status'])
            set_vehicle_availability(r.vehicle)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Expired {count} pending rentals."))