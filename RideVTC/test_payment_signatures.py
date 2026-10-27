import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.test.client import RequestFactory
from RideVTC.utils.payments import verify_and_parse
from RideVTC.callbacks import ProviderCallback


@override_settings(AIRTEL_MONEY={"WEBHOOK_SECRET": "test-secret"})
class PaymentSignatureTests(SimpleTestCase):
    rejected = (False, None, None, None)

    def request(self, body=None, signature=True, header="HTTP_X_AIRTEL_SIGNATURE"):
        raw = body if isinstance(body, bytes) else json.dumps(body or {"transactionId": "tx-1", "status": "SUCCESS", "reference": "ref-1"}).encode()
        headers = {}
        if signature is True:
            headers[header] = base64.b64encode(hmac.new(b"test-secret", raw, hashlib.sha256).digest()).decode()
        elif isinstance(signature, str):
            headers[header] = signature
        return RequestFactory().post("/callback/", raw, content_type="application/json", **headers)

    def test_valid_signature(self):
        self.assertEqual(verify_and_parse(self.request()), (True, "tx-1", "SUCCESS", "ref-1"))

    def test_alternate_header(self):
        self.assertTrue(verify_and_parse(self.request(header="HTTP_X_SIGNATURE"))[0])

    def test_missing_signature(self):
        self.assertEqual(verify_and_parse(self.request(signature=False)), self.rejected)

    def test_invalid_and_non_ascii_signatures(self):
        for signature in ("incorrect", "é", ""):
            with self.subTest(signature=signature):
                self.assertEqual(verify_and_parse(self.request(signature=signature)), self.rejected)

    def test_missing_secret(self):
        with override_settings(AIRTEL_MONEY={"WEBHOOK_SECRET": None}):
            self.assertEqual(verify_and_parse(self.request()), self.rejected)

    def test_altered_body(self):
        req = self.request({"transactionId": "tx-1", "status": "FAILED"})
        req.META["HTTP_X_AIRTEL_SIGNATURE"] = self.request().META["HTTP_X_AIRTEL_SIGNATURE"]
        self.assertEqual(verify_and_parse(req), self.rejected)

    def test_malformed_signed_payloads(self):
        for body in (b"invalid", b"[]", b"null", b"{\"transaction\": []}", b"{\"transactionId\": \"tx\"}", b"{\"status\": \"SUCCESS\"}"):
            with self.subTest(body=body):
                self.assertEqual(verify_and_parse(self.request(body)), self.rejected)

    def test_nested_status_and_reference(self):
        req = self.request({"transaction": {"status": "SUCCESS", "reference": "ref-1"}})
        self.assertEqual(verify_and_parse(req), (True, None, "SUCCESS", "ref-1"))

    def test_unsigned_callback_does_not_query_payments(self):
        with patch("RideVTC.callbacks.Payment.objects.filter") as lookup:
            response = ProviderCallback.as_view()(self.request(signature=False))
        self.assertEqual(response.status_code, 400)
        lookup.assert_not_called()

    def test_unsigned_reference_header_cannot_select_payment(self):
        req = self.request({"status": "SUCCESS"})
        req.META["HTTP_X_REFERENCE_ID"] = "another-payment"
        self.assertEqual(verify_and_parse(req), self.rejected)

    def test_nested_transaction_id_is_parsed(self):
        req = self.request({"transaction": {"id": "tx-nested", "status": "SUCCESS", "reference": "ref-1"}})
        self.assertEqual(verify_and_parse(req), (True, "tx-nested", "SUCCESS", "ref-1"))
