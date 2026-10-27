"""Signed callback integration tests; no provider or real payment is contacted."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.test.client import RequestFactory
from django.utils import timezone

from users.models import CustomUser
from RideVTC.models import Payment, Ride
from RideVTC.callbacks import ProviderCallback
from vehicles.models import Rental, RentalPayment, Vehicle
from vehicles.views import RentalProviderCallback


class CallbackCases:
    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create(email="callback-test@example.com", user_type="client")
        self.payment = self.make_payment("ref-1", "tx-1")

    def request(self, status="SUCCESS", txid="tx-1", reference="ref-1", signed=True):
        body = {"status": status}
        if txid is not None:
            body["transactionId"] = txid
        if reference is not None:
            body["reference"] = reference
        raw = json.dumps(body).encode()
        headers = {}
        if signed:
            headers["HTTP_X_AIRTEL_SIGNATURE"] = base64.b64encode(hmac.new(b"test-secret", raw, hashlib.sha256).digest()).decode()
        return RequestFactory().post("/callback/", raw, content_type="application/json", **headers)

    def callback(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.view.as_view()(self.request(**kwargs))
        self.payment.refresh_from_db()
        return response

    def test_success(self):
        self.assertEqual(self.callback().status_code, 200)
        self.assertEqual(self.payment.status, "SUCCESS")

    def test_failed(self):
        self.assertEqual(self.callback(status="FAILED").status_code, 200)
        self.assertEqual(self.payment.status, "FAILED")

    def test_repeated_success_does_not_write_again(self):
        self.callback()
        updated_at = self.payment.updated_at
        self.assertEqual(self.callback().status_code, 200)
        self.assertEqual(self.payment.updated_at, updated_at)

    def test_delayed_pending_does_not_undo_success(self):
        self.callback()
        response = self.callback(status="PENDING")
        self.assertEqual(response.data["status"], "SUCCESS")
        self.assertEqual(self.payment.status, "SUCCESS")

    def test_delayed_failure_does_not_undo_success(self):
        self.callback()
        self.callback(status="FAILED")
        self.assertEqual(self.payment.status, "SUCCESS")

    def test_delayed_pending_does_not_undo_failure(self):
        self.callback(status="FAILED")
        self.callback(status="PENDING")
        self.assertEqual(self.payment.status, "FAILED")

    def test_success_after_uncertain_failure_is_recorded(self):
        self.callback(status="FAILED")
        self.callback()
        self.assertEqual(self.payment.status, "SUCCESS")

    def test_unsigned_rejected_without_changes(self):
        self.assertEqual(self.callback(signed=False).status_code, 400)
        self.assertEqual(self.payment.status, "PENDING")

    def test_unknown_payment_returns_not_found(self):
        self.assertEqual(self.callback(txid="missing", reference="missing").status_code, 404)
        self.assertEqual(self.payment.status, "PENDING")

    def test_reference_only_callback(self):
        self.assertEqual(self.callback(txid=None).status_code, 200)
        self.assertEqual(self.payment.status, "SUCCESS")

    def test_txid_only_callback(self):
        self.assertEqual(self.callback(reference=None).status_code, 200)
        self.assertEqual(self.payment.status, "SUCCESS")

    def test_reference_links_transaction_when_init_did_not_return_id(self):
        self.payment.provider_txid = None
        self.payment.save(update_fields=["provider_txid"])
        self.assertEqual(self.callback(txid="new-tx").status_code, 200)
        self.assertEqual(self.payment.provider_txid, "new-tx")

    def test_legacy_reference_stored_as_transaction_can_be_replaced(self):
        self.payment.provider_txid = self.payment.idempotency_key
        self.payment.save(update_fields=["provider_txid"])
        self.assertEqual(self.callback(txid="real-provider-id").status_code, 200)
        self.assertEqual(self.payment.provider_txid, "real-provider-id")

    def test_init_without_provider_id_keeps_reference_separate(self):
        from RideVTC.utils.payments import provider_init_payment
        self.payment.provider = "AIRTEL_MONEY"
        self.payment.provider_txid = None
        self.payment.save(update_fields=["provider", "provider_txid"])
        with patch("RideVTC.utils.payments.AirtelAfricaClient") as provider:
            provider.return_value.initiate_collection.return_value = (True, None, "OK")
            self.assertEqual(provider_init_payment(self.payment), (True, None, "OK"))
        self.payment.refresh_from_db()
        self.assertIsNone(self.payment.provider_txid)
        self.assertEqual(self.callback(txid="real-provider-id").status_code, 200)

    def test_conflicting_reference_rejected(self):
        self.make_payment("ref-2", "tx-2")
        self.assertEqual(self.callback(reference="ref-2").status_code, 409)
        self.assertEqual(self.payment.status, "PENDING")

    def test_conflicting_transaction_rejected(self):
        self.assertEqual(self.callback(txid="other-tx").status_code, 409)
        self.assertEqual(self.payment.status, "PENDING")

    def test_duplicate_provider_transaction_is_not_arbitrarily_selected(self):
        self.make_payment("ref-2", "tx-1")
        self.assertEqual(self.callback(reference=None).status_code, 409)
        self.assertEqual(self.payment.status, "PENDING")

    def test_reference_only_does_not_select_unrelated_null_txid(self):
        other = self.make_payment("ref-2", None)
        self.payment.provider_txid = None
        self.payment.save(update_fields=["provider_txid"])
        self.assertEqual(self.callback(txid=None).status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.status, "PENDING")
        self.assertEqual(self.payment.status, "SUCCESS")


@override_settings(AIRTEL_MONEY={"WEBHOOK_SECRET": "test-secret"})
class RideCallbackTests(CallbackCases, TestCase):
    view = ProviderCallback

    def make_payment(self, reference, txid):
        ride = Ride.objects.create(user=self.owner, pickup_location="A", dropoff_location="B", distance_km=1, price=1000)
        return Payment.objects.create(ride=ride, amount=1000, wallet="MOBILE_MONEY", idempotency_key=reference, provider_txid=txid)

    def test_event_emitted_once_after_commit(self):
        with patch("RideVTC.callbacks.app_ws_send") as notify:
            self.callback()
            self.callback()
            self.assertEqual(notify.call_count, 1)

    def test_no_event_when_payment_transaction_rolls_back(self):
        from django.db import transaction
        with patch("RideVTC.callbacks.app_ws_send") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.view.as_view()(self.request())
                        raise RuntimeError("force rollback")
                except RuntimeError:
                    pass
            notify.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "PENDING")

    def test_legacy_view_import_uses_same_implementation(self):
        # Parse this legacy path without loading unrelated Firebase dependencies.
        import ast
        from pathlib import Path
        tree = ast.parse(Path(__file__).with_name("views.py").read_text())
        imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
        self.assertTrue(any(node.module == "RideVTC.callbacks" and any(alias.name == "ProviderCallback" for alias in node.names) for node in imports))
        self.assertFalse(any(isinstance(node, ast.ClassDef) and node.name == "ProviderCallback" for node in tree.body))


@override_settings(AIRTEL_MONEY={"WEBHOOK_SECRET": "test-secret"})
class RentalCallbackTests(CallbackCases, TestCase):
    view = RentalProviderCallback

    def make_payment(self, reference, txid):
        vehicle = Vehicle.objects.create(brand="Test", model="Test", transmission="manual", fuel_type="essence", seats=4, registration_number=reference, daily_price=1000, city="Libreville", category="eco")
        now = timezone.now()
        rental = Rental.objects.create(user=self.owner, vehicle=vehicle, start_date=now, end_date=now+timedelta(days=1), total_amount=1000)
        return RentalPayment.objects.create(rental=rental, amount=1000, idempotency_key=reference, provider_txid=txid)

    def test_success_confirms_rental_and_repeated_callback_keeps_code(self):
        self.callback()
        rental = self.payment.rental
        rental.refresh_from_db()
        self.assertEqual(rental.status, "confirmed")
        self.assertEqual(rental.payment_method, "mobile")
        self.assertEqual(rental.total_amount, 1000)
        self.assertTrue(rental.identification_code)
        code = rental.identification_code
        self.callback()
        rental.refresh_from_db()
        self.assertEqual(rental.identification_code, code)

    def test_failed_payment_does_not_confirm_rental(self):
        self.callback(status="FAILED")
        self.payment.rental.refresh_from_db()
        self.assertEqual(self.payment.rental.status, "pending")
        self.assertEqual(self.payment.rental.payment_method, "")

    def test_rental_update_failure_rolls_back_payment_too(self):
        with patch("vehicles.models.Rental.mark_paid_mobile", side_effect=RuntimeError("save failed")):
            with self.assertRaises(RuntimeError):
                self.callback()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "PENDING")

    def test_late_success_does_not_reopen_canceled_or_expired_rental(self):
        for closed in ("canceled", "expired"):
            with self.subTest(status=closed):
                self.payment.status = "PENDING"
                self.payment.save(update_fields=["status"])
                rental = self.payment.rental
                rental.status = closed
                rental.save(update_fields=["status"])
                self.assertEqual(self.callback().status_code, 200)
                rental.refresh_from_db()
                self.assertEqual(rental.status, closed)
                self.assertEqual(self.payment.status, "SUCCESS")
                self.assertTrue(self.payment.meta["requires_review"])
