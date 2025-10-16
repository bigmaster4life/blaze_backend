# RideVTC/callbacks.py
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

        # On cherche d'abord par provider_txid, sinon par idempotency/reference
        p = None
        if provider_txid:
            p = Payment.objects.filter(provider_txid=provider_txid).first()
        if not p and reference:
            p = Payment.objects.filter(idempotency_key=reference).first()

        if not p:
            # pas trouvé → on renvoie 200 pour éviter les retries agressifs, mais on logge côté serveur
            return Response({"ok": True, "warn": "payment_not_found"}, status=200)

        new_status = map_status(provider_status)
        if p.status != new_status:
            p.status = new_status
            p.save(update_fields=["status"])

            # Optionnel: pousser une notif temps réel
            try:
                app_ws_send({
                    "type": "payment.status",
                    "rideId": p.ride_id,
                    "paymentId": p.id,
                    "status": new_status,
                    "amount": str(p.amount),
                    "ref": p.idempotency_key,
                    "txid": p.provider_txid,
                })
            except Exception:
                pass

        return Response({"ok": True, "status": new_status})