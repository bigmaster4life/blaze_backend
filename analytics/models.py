from django.db import models

# Create your models here.

from django.conf import settings


class CancellationRecord(models.Model):
    """Private audit event; never included in partner notifications/serializers."""
    ride = models.OneToOneField('RideVTC.Ride', null=True, blank=True, on_delete=models.PROTECT, related_name='cancellation_record')
    delivery = models.OneToOneField('Delivery.DeliveryRequest', null=True, blank=True, on_delete=models.PROTECT, related_name='cancellation_record')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    partner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    analysis_pending = models.BooleanField(default=True, db_index=True)
    actor_role = models.CharField(max_length=16)
    reason = models.CharField(max_length=40)
    note = models.CharField(max_length=500, blank=True)
    status_before = models.CharField(max_length=24)
    context = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(ride__isnull=False, delivery__isnull=True) | models.Q(ride__isnull=True, delivery__isnull=False)), name='cancellation_exactly_one_service')]


class CancellationAlert(models.Model):
    record = models.OneToOneField(CancellationRecord, on_delete=models.CASCADE, related_name='alert')
    score = models.PositiveSmallIntegerField()
    signals = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default='open', db_index=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)


class CancellationReview(models.Model):
    alert = models.ForeignKey(CancellationAlert, on_delete=models.CASCADE, related_name='reviews')
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    previous_status = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    note = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)
