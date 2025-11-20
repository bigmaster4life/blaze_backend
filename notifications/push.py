import json
import logging
import requests
from typing import Iterable, Optional, Dict, Any
from django.conf import settings
from .models import Device

logger = logging.getLogger(__name__)

FCM_ENDPOINT = "https://fcm.googleapis.com/fcm/send"

def _server_key() -> str:
    key = getattr(settings, "FCM_SERVER_KEY", "") or ""
    if not key:
        logger.error("[FCM] FCM_SERVER_KEY manquant dans settings")
    return key

def _send(payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"key={_server_key()}",
    }
    try:
        r = requests.post(FCM_ENDPOINT, headers=headers, data=json.dumps(payload), timeout=15)
        r.raise_for_status()
        j = r.json()
        logger.info("[FCM] success=%s failure=%s", j.get("success"), j.get("failure"))
        return j
    except Exception as e:
        logger.exception("[FCM] send error: %s", e)
        return {"success": 0, "failure": 0, "error": str(e)}

def send_fcm_to_tokens(
    tokens: Iterable[str],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    *,
    android_channel_id: str = "blaze_general",
    sound: str = "default",  # Android: "notify"; iOS: "notify.caf"
    priority: str = "high",
) -> Dict[str, Any]:
    tokens = [t for t in (tokens or []) if t]
    if not tokens:
        return {"success": 0, "failure": 0, "message": "no tokens"}
    payload = {
        "registration_ids": tokens,
        "priority": priority,
        "notification": {
            "title": title,
            "body": body,
            "sound": sound,
            "android_channel_id": android_channel_id,  # heads-up + son canal
        },
        "data": data or {},
    }
    # Optionnel: doublure au format "android.notification"
    payload["android"] = {"notification": {"channel_id": android_channel_id}}
    return _send(payload)

def send_fcm_to_user(user_id: int, title: str, body: str, data=None, *, sound: str = "default"):
    tokens = list(Device.objects.filter(user_id=user_id, is_active=True).values_list("token", flat=True))
    return send_fcm_to_tokens(tokens, title, body, data, sound=sound)

def send_fcm_to_driver(driver_id: int, title: str, body: str, data=None, *, sound: str = "default"):
    tokens = list(Device.objects.filter(driver_id=driver_id, is_active=True).values_list("token", flat=True))
    return send_fcm_to_tokens(tokens, title, body, data, sound=sound)