# RideVTC/callbacks.py
from django.db import transaction
from RideVTC.payment_callbacks import CallbackIdentityConflict, locked_payment, update_payment
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView
from rest_framework.response import Response

from RideVTC.utils.payments import verify_and_parse, map_status, app_ws_send
from RideVTC.models import Payment


@method_decorator(csrf_exempt, name="dispatch")  # pas de CSRF pour le webhook provider
class ProviderCallback(APIView):
    authentication_classes = []  # webhook public (sécurisé par signature)
    permission_classes = []

    def post(self, request, *args, **kwargs):
        ok, provider_txid, provider_status, reference = verify_and_parse(request)
        if not ok:
            return Response({"ok": False, "error": "bad_signature_or_payload"}, status=400)

        try:
            with transaction.atomic():
                p = locked_payment(Payment, provider_txid, reference)
                if p is None:
                    return Response({"ok": False, "error": "payment_not_found"}, status=404)
                changed = update_payment(p, map_status(provider_status), provider_txid)
                if changed:
                    payload = {
                        "type": "payment.status", "rideId": p.ride_id,
                        "paymentId": p.id, "status": p.status, "amount": str(p.amount),
                        "ref": p.idempotency_key, "txid": p.provider_txid,
                    }
                    # Emit only after the database commit, once per status transition.
                    transaction.on_commit(lambda: app_ws_send(payload), robust=True)
        except CallbackIdentityConflict:
            return Response({"ok": False, "error": "payment_identity_conflict"}, status=409)
        return Response({"ok": True, "status": p.status})
