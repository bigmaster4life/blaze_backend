# RideVTC/utils/payments.py
import uuid
import json
import hmac
import base64
from hashlib import sha256
from decimal import Decimal
from typing import Tuple, Optional

import requests
from django.conf import settings

from RideVTC.models import Payment


# -------------------------
# Helpers communs
# -------------------------

def normalize_msisdn(raw: str, default_cc: str = "241") -> str:
    """
    Normalise MSISDN au format international sans + (ex: '2416XXXXXXXX').
    Adapte default_cc à ton pays (GA=241).
    """
    if not raw:
        return ""
    s = "".join(ch for ch in str(raw) if ch.isdigit())
    if s.startswith("00"):
        s = s[2:]
    if s.startswith("0"):
        s = default_cc + s[1:]
    return s


def select_provider(_ride) -> str:
    """Force Airtel Money pour l’instant."""
    return "AIRTEL_MONEY"


def _get_cfg() -> dict:
    """
    On accepte 2 styles de config:
      - settings.AIRTEL_MONEY = { ... }
      - variables plates: AIRTEL_MONEY_* (fallback)
    """
    d = getattr(settings, "AIRTEL_MONEY", {}) or {}
    if d:
        return d
    return {
        "BASE_URL": getattr(settings, "AIRTEL_MONEY_BASE_URL", "").rstrip("/"),
        "CLIENT_ID": getattr(settings, "AIRTEL_MONEY_CLIENT_ID", ""),
        "CLIENT_SECRET": getattr(settings, "AIRTEL_MONEY_CLIENT_SECRET", ""),
        "COUNTRY": getattr(settings, "AIRTEL_MONEY_COUNTRY", "GA"),
        "CURRENCY": getattr(settings, "AIRTEL_MONEY_CURRENCY", "XAF"),
        "CALLBACK_URL": f"{getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')}/api/payments/mobile/callback/",
        "SUBSCRIPTION_KEY": getattr(settings, "AIRTEL_MONEY_SUBSCRIPTION_KEY", None),
        "TARGET_ENV": getattr(settings, "AIRTEL_MONEY_TARGET_ENV", "sandbox"),
        "WEBHOOK_SECRET": getattr(settings, "AIRTEL_MONEY_CALLBACK_SECRET", None),
        "PATH_OAUTH": "/auth/oauth2/token",
        "PATH_INIT": "/merchant/v1/payments/initiate",
    }


# -------------------------
# Airtel Money client
# -------------------------

class AirtelAfricaClient:
    def __init__(self):
        cfg = _get_cfg()
        self.base = cfg.get("BASE_URL", "").rstrip("/")
        self.client_id = cfg.get("CLIENT_ID", "")
        self.client_secret = cfg.get("CLIENT_SECRET", "")
        self.country = cfg.get("COUNTRY", "GA")
        self.currency = cfg.get("CURRENCY", "XAF")
        self.callback_url = cfg.get("CALLBACK_URL", "")
        self.subscription_key = cfg.get("SUBSCRIPTION_KEY")
        self.target_env = cfg.get("TARGET_ENV", "sandbox")
        self.paths = {
            "oauth": cfg.get("PATH_OAUTH", "/auth/oauth2/token"),
            "init":  cfg.get("PATH_INIT",  "/merchant/v1/payments/initiate"),
        }

    # -------- OAuth --------
    def get_access_token(self) -> str:
        url = f"{self.base}{self.paths['oauth']}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        data = r.json() if r.content else {}
        return data.get("access_token") or data.get("accessToken") or ""

    # -------- Init collection --------
    def initiate_collection(
        self, *, amount: Decimal, msisdn: str, reference: str
    ) -> Tuple[bool, Optional[str], str]:
        token = self.get_access_token()
        url = f"{self.base}{self.paths['init']}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Country": self.country,
            "X-Currency": self.currency,
            "X-Reference-Id": reference,           # idempotency côté Airtel
            "X-Target-Environment": self.target_env,
        }
        cfg = _get_cfg()
        if cfg.get("SUBSCRIPTION_KEY"):
            headers["Ocp-Apim-Subscription-Key"] = cfg["SUBSCRIPTION_KEY"]

        payload = {
            "reference": reference,  # on l’envoie aussi dans le body si accepté
            "subscriber": {
                "country": self.country,
                "currency": self.currency,
                "msisdn": msisdn,
            },
            "transaction": {
                "amount": str(amount),
                "country": self.country,
                "currency": self.currency,
            },
            # Certains tenants utilisent redirectUrl, d’autres s’appuient uniquement sur le webhook serveur
            "redirectUrl": self.callback_url,
        }

        try:
            r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=45)
            if r.status_code not in (200, 201, 202):
                msg = f"HTTP {r.status_code}"
                try:
                    msg = (r.json() or {}).get("message") or msg
                except Exception:
                    pass
                return (False, None, msg)

            data = r.json() if r.content else {}
            # Plusieurs variantes possibles
            provider_txid = (
                data.get("transactionId")
                or data.get("airtelMoneyId")
                or data.get("id")
                or None
            )
            return (True, provider_txid, "OK")
        except requests.RequestException as e:
            return (False, None, str(e))


# -------------------------
# Pont “provider_*” utilisés par tes vues
# -------------------------

def _ensure_keys(p: Payment) -> None:
    """Crée une clé d'idempotence si absente."""
    if not p.idempotency_key:
        p.idempotency_key = f"vtc-{p.ride_id}-{uuid.uuid4().hex[:12]}"
        p.save(update_fields=["idempotency_key"])


def provider_init_payment(p: Payment) -> Tuple[bool, Optional[str], str]:
    """
    Déclenche le paiement côté provider.
    - Sauvegarde idempotency_key si absente
    - Met à jour provider_txid si retourné
    """
    _ensure_keys(p)

    if p.provider == "AIRTEL_MONEY":
        client = AirtelAfricaClient()
        ok, txid, msg = client.initiate_collection(
            amount=p.amount,
            msisdn=normalize_msisdn(p.msisdn),
            reference=p.idempotency_key,
        )
        if ok:
            # on persiste le lien vers la transac provider (si dispo)
            if txid and txid != p.provider_txid:
                p.provider_txid = txid
                p.save(update_fields=["provider_txid"])
        return ok, (txid or p.idempotency_key), msg

    return False, None, "Unsupported provider"


# -------------------------
# Webhook utils (callback)
# -------------------------

def verify_and_parse(request) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Vérifie la signature HMAC (si configurée) et retourne:
      (ok, provider_txid, provider_status, reference)
    """
    raw = request.body or b""
    body = {}
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        body = {}

    # champs possibles selon les tenants / versions
    provider_txid = (
        body.get("transactionId")
        or body.get("airtelMoneyId")
        or body.get("id")
        or None
    )
    provider_status = (
        body.get("status")
        or (body.get("transaction") or {}).get("status")
        or None
    )
    reference = (
        body.get("reference")
        or (body.get("transaction") or {}).get("reference")
        or request.headers.get("X-Reference-Id")
        or None
    )

    # Signature HMAC optionnelle
    cfg = _get_cfg()
    secret = cfg.get("WEBHOOK_SECRET")
    header_sig = request.headers.get("X-Airtel-Signature") or request.headers.get("X-Signature")
    if secret and header_sig:
        mac = hmac.new(secret.encode("utf-8"), raw, sha256).digest()
        calc = base64.b64encode(mac).decode("utf-8")
        if calc != header_sig:
            return (False, None, None, None)

    return (True, provider_txid, provider_status, reference)


def map_status(provider_status: Optional[str]) -> str:
    if not provider_status:
        return "PENDING"
    s = str(provider_status).lower()
    if s in {"success", "successful", "succeeded", "paid", "completed"}:
        return "SUCCESS"
    if s in {"failed", "declined", "rejected", "canceled", "cancelled", "timeout"}:
        return "FAILED"
    return "PENDING"


def app_ws_send(payload: dict):
    """No-op; branche sur Channels si nécessaire."""
    try:
        # from channels.layers import get_channel_layer
        # from asgiref.sync import async_to_sync
        # channel_layer = get_channel_layer()
        # async_to_sync(channel_layer.group_send)("customer_updates", {"type": "app.event", "data": payload})
        pass
    except Exception:
        pass