import os
import json
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from notifications.models import Device
import logging
logger = logging.getLogger(__name__)

# ==============================
# CONFIG
# ==============================

FCM_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

FCM_SERVICE_ACCOUNT_FILE = os.getenv(
    "FCM_SERVICE_ACCOUNT_FILE",
    "firebase-service-account.json"
)

FCM_PROJECT_ID = os.getenv("FCM_PROJECT_ID", "blazemobile-f25f7")

FCM_ENDPOINT = f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send"


# ==============================
# AUTH
# ==============================

def get_access_token():
    credentials = service_account.Credentials.from_service_account_file(
        FCM_SERVICE_ACCOUNT_FILE, scopes=FCM_SCOPES
    )
    credentials.refresh(Request())
    return credentials.token


# ==============================
# CORE SEND
# ==============================

def send_fcm_message(token: str, payload: dict) -> bool:
    try:
        access_token = get_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; UTF-8",
        }

        body = {
            "message": {
                "token": token,
                **payload
            }
        }

        response = requests.post(
            FCM_ENDPOINT,
            headers=headers,
            json=body,  # ✅ mieux que data=json.dumps
            timeout=10
        )

        if response.status_code == 200:
            print("✅ FCM SENT:", token[:12])
            return True
        
        txt = response.text or ""
        if "UNREGISTERED" in txt or "registration-token-not-registered" in txt:
            print("🧹 FCM TOKEN DEAD → disable:", token[:12])
            Device.objects.filter(token=token).update(is_active=False)
            return False

        print("❌ FCM ERROR:", response.status_code, txt)
        return False

    except Exception as e:
        print("❌ FCM EXCEPTION:", str(e))
        return False


# ==============================
# PAYLOADS
# ==============================

def payload_vtc_new_ride(ride_id: int):
    return {
        "notification": {
            "title": "Nouvelle course",
            "body": "Un client demande une course"
        },
        "data": {
            "type": "ride.new",
            "ride_id": str(ride_id),
        },
        "android": {
            "priority": "HIGH",
            "ttl": "60s",
            "notification": {
                "channel_id": "be_driver_alerts",
                "sound": "default",
                "visibility": "PUBLIC",
                "tag": f"ride_{ride_id}"
            }
        }
    }


# ==============================
# ✅ AJOUT LIVREUR – PAYLOAD COMPLET
# ==============================

def payload_delivery_new_order(delivery_request):
    details = getattr(delivery_request, 'order_details', None) or {}
    merchant = details.get('merchant_name') if isinstance(details, dict) else None
    pickup = merchant or delivery_request.pickup_label
    return {
        "notification": {
            "title": "Nouvelle livraison",
            "body": f"{pickup} → {delivery_request.dropoff_label}"
        },
        "android": {
            "priority": "HIGH",
            "ttl": "60s",
            "notification": {
                "channel_id": "delivery",
                "tag": f"delivery_{delivery_request.id}",
                "visibility": "PUBLIC",
                "sound": "default"
            }
        },
        "data": {
            "type": "delivery.new",
            "delivery_request_id": str(delivery_request.id)
        }
    }


def payload_client_status_update(object_type: str, object_id: int, status: str):
    return {
        "data": {
            "type": "client.update",
            "object_type": object_type,  # ride | delivery
            "object_id": str(object_id),
            "status": status,
            "title": "Mise à jour",
            "body": f"Statut : {status}"
        },
        "android": {
            "priority": "HIGH"
        }
    }


# ==============================
# HIGH LEVEL HELPERS
# ==============================

def notify_driver_new_ride(driver, ride_id: int):
    logger.warning("FCM START → driver_id=%s ride_id=%s", driver.id, ride_id)
    tokens = list(
        Device.objects.filter(user=driver, is_active=True)
        .values_list("token", flat=True)
    )
    logger.warning("FCM TOKENS FOUND → %s", len(tokens))

    if not tokens:
        logger.warning("⚠️ Aucun device chauffeur actif")
        return False
    payload = payload_vtc_new_ride(ride_id)

    sent = False
    for token in tokens:
        logger.warning("FCM SENDING TO → %s", token[:12])
        ok = send_fcm_message(token, payload)
        logger.warning("FCM RESULT → %s", ok)
        sent = sent or ok
    return sent


# ==============================
# ✅ AJOUT LIVREUR – NOTIFICATION NOUVELLE DEMANDE
# ==============================

def notify_delivery_driver_new_order(driver, delivery_request):
    """
    Envoie à TOUS les téléphones du livreur
    (app ouverte, background, tuée)
    """

    tokens = list(
        Device.objects.filter(user=driver.user, is_active=True)
        .values_list("token", flat=True)
    )
    if not tokens:
        print("⚠️ Aucun device enregistré pour ce livreur")
        return False

    payload = payload_delivery_new_order(delivery_request)

    sent = False
    for token in tokens:
        ok = send_fcm_message(token, payload)
        sent = sent or ok
    return sent


def notify_client_update(client, object_type: str, object_id: int, status: str):
    if not client.fcm_token:
        return False
    payload = payload_client_status_update(object_type, object_id, status)
    return send_fcm_message(client.fcm_token, payload)


def payload_chat_message(title: str, body: str, ride_id: int, sender: str):
    return {
        "notification": {
            "title": title,
            "body": body
        },
        "android": {
            "priority": "HIGH",
            "ttl": "60s",
            "notification": {
                "channel_id": "chat",
                "sound": "default",
                "visibility": "PUBLIC"
            }
        },
        "data": {
            "type": "ride.chat",
            "ride_id": str(ride_id),
            "sender": sender
        }
    }

def notify_chat_message(user, title, body, ride_id, sender):
    tokens = list(
        Device.objects.filter(user=user, is_active=True)
        .values_list("token", flat=True)
    )
    if not tokens:
        return False

    payload = payload_chat_message(title, body, ride_id, sender)

    sent = False
    for token in tokens:
        ok = send_fcm_message(token, payload)
        sent = sent or ok

    return sent