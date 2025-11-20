from django.conf import settings
from django.db import models

# Optionnel : FK Driver si tu en as un
try:
    from drivers.models import Driver
except Exception:
    Driver = None

class Device(models.Model):
    ANDROID = "android"
    IOS = "ios"
    WEB = "web"
    PLATFORM_CHOICES = [(ANDROID, "Android"), (IOS, "iOS"), (WEB, "Web")]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="devices",
        null=True, blank=True,  # ← permet d’enregistrer pour un driver
    )
    # new: driver cible (optionnel)
    driver = models.ForeignKey(
        Driver,
        on_delete=models.CASCADE,
        related_name="devices",
        null=True, blank=True,
    )

    token = models.TextField(unique=True)
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, default=ANDROID)
    device_id = models.CharField(max_length=64, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)   # sert de last_seen

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["driver"]),
            models.Index(fields=["token"]),
        ]

    def __str__(self):
        owner = self.user_id or self.driver_id or "?"
        return f"Device<{self.platform}> token={self.token[:10]}… owner={owner}"