from django.http import JsonResponse, HttpResponse
from django.db import connection
from django.conf import settings
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model

def healthz(_request):
    return JsonResponse({"status": "ok"}, status=200)

def readyz(_request):
    return HttpResponse("ok", content_type="text/plain", status=200)

def healthz_full(_request):
    checks = {"http": True}
    # DB
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        checks["db"] = True
    except Exception as e:
        checks["db"] = False
        checks["db_error"] = str(e)

    # JWT (génération token sur un user existant si dispo)
    try:
        User = get_user_model()
        u = User.objects.order_by("id").first()
        if u:
            RefreshToken.for_user(u)  # simple smoke test
            checks["jwt"] = True
        else:
            checks["jwt"] = "no-user"
    except Exception as e:
        checks["jwt"] = False
        checks["jwt_error"] = str(e)

    # Channels (juste présence et layer type)
    try:
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        checks["channels"] = bool(layer is not None)
        checks["channels_backend"] = settings.CHANNEL_LAYERS["default"]["BACKEND"]
    except Exception as e:
        checks["channels"] = False
        checks["channels_error"] = str(e)

    # Google Maps key (présence)
    checks["google_maps_key_present"] = bool(getattr(settings, "GOOGLE_MAPS_API_KEY", ""))

    status = 200 if all(v in (True, "no-user") for v in checks.values() if not isinstance(v, str)) else 503
    return JsonResponse(checks, status=status)