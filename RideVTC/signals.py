from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Payment
from .accounting import record_allocation


@receiver(post_save, sender=Payment, dispatch_uid='ridevtc.freeze_payment_allocation')
def freeze_payment_allocation(sender, instance, raw=False, **kwargs):
    if not raw:
        record_allocation(instance)
