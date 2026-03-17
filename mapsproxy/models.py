from django.db import models

class PlaceLabel(models.Model):
    lat = models.FloatField()
    lng = models.FloatField()
    label = models.CharField(max_length=255)

    city = models.CharField(max_length=64, blank=True, null=True)  # libreville, etc
    source = models.CharField(max_length=32, default='user')       # user / admin / import
    confidence = models.IntegerField(default=70)                   # 0-100 (simple)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['lat', 'lng']),
            models.Index(fields=['city']),
        ]

    def __str__(self):
        return f"{self.label} ({self.lat},{self.lng})"
