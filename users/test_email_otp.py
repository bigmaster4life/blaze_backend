import re
from smtplib import SMTPAuthenticationError, SMTPServerDisconnected
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CustomUser, EmailChallenge


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailOTPTests(TestCase):
    phone = "+241074562847"
    email = "tester@example.com"

    def setUp(self):
        self.client = APIClient()

    def request_code(self, purpose="signup", email=None):
        return self.client.post("/auth/request-otp-email/", {"phone_number": self.phone, "email": email or self.email, "purpose": purpose}, format="json")

    def code(self):
        return re.search(r"\b\d{6}\b", mail.outbox[-1].body).group()

    def verify(self, code=None, email=None):
        return self.client.post("/auth/verify-signup-email/", {"phone": self.phone, "email": email or self.email, "code": code or self.code()}, format="json")

    def account(self, verified=True):
        from .utils import normalize_phone_gabon
        user = CustomUser(email=self.email, phone_number=normalize_phone_gabon(self.phone), email_verified=verified)
        user.set_password("Old-password-729!")
        user.save()
        return user

    def test_signup_records_verified_email_and_unusable_password(self):
        response = self.request_code()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("otp", response.data)
        response = self.verify()
        self.assertEqual(response.status_code, 200, response.data)
        user = CustomUser.objects.get(email=self.email)
        self.assertTrue(user.email_verified)
        self.assertFalse(user.has_usable_password())
        self.assertIn("access", response.data)
        self.assertFalse(EmailChallenge.objects.exists())
        self.assertEqual(self.verify().status_code, 400)

    def test_existing_account_rejects_arbitrary_email(self):
        self.account()
        self.assertEqual(self.request_code("reset", "attacker@example.com").status_code, 400)
        self.assertFalse(EmailChallenge.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_legacy_account_rejects_unverified_email(self):
        self.account(verified=False)
        self.assertEqual(self.request_code("reset").status_code, 400)

    def test_existing_password_account_cannot_use_signup(self):
        self.account()
        self.assertEqual(self.request_code().status_code, 400)

    def test_reset_changes_password_without_issuing_login_token(self):
        user = self.account()
        self.assertEqual(self.request_code("reset").status_code, 200)
        response = self.client.post("/auth/verify-otp-email/", {"phone": self.phone, "email": self.email, "code": self.code(), "new_password": "New-password-821!", "confirm_password": "New-password-821!"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("access", response.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("New-password-821!"))
        self.assertFalse(EmailChallenge.objects.exists())

    def test_reset_code_cannot_be_used_for_signup(self):
        self.account()
        self.request_code("reset")
        self.assertEqual(self.verify().status_code, 400)
        self.assertEqual(EmailChallenge.objects.count(), 1)

    def test_invalid_attempts_are_limited(self):
        self.request_code()
        code = self.code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(self.verify(wrong).status_code, 400)
        self.assertEqual(EmailChallenge.objects.get().attempts, 5)
        self.assertEqual(self.verify(code).status_code, 400)

    def test_expired_code_is_rejected(self):
        self.request_code()
        EmailChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify().status_code, 400)
        self.assertFalse(CustomUser.objects.exists())

    def test_code_bound_to_email_and_phone(self):
        self.request_code()
        self.assertEqual(self.verify(email="other@example.com").status_code, 400)
        self.assertFalse(CustomUser.objects.exists())

    def test_resend_wait_and_replaces_code(self):
        self.request_code()
        self.assertEqual(self.request_code().status_code, 400)
        old_code = self.code()
        EmailChallenge.objects.update(created_at=timezone.now() - timedelta(seconds=61))
        replacement = 111111 if old_code != "111111" else 222222
        with patch("users.email_otp.secrets.randbelow", return_value=replacement):
            self.assertEqual(self.request_code().status_code, 200)
        self.assertEqual(self.verify(old_code).status_code, 400)
        self.assertEqual(self.verify().status_code, 200)

    def test_mail_failure_does_not_leave_challenge(self):
        self.client.raise_request_exception = False
        with patch("users.email_otp.send_mail", side_effect=SMTPServerDisconnected("mail unavailable")):
            self.assertEqual(self.request_code().status_code, 503)
        self.assertFalse(EmailChallenge.objects.exists())

    def test_reset_password_confirmation_preserves_code(self):
        user = self.account()
        self.request_code("reset")
        response = self.client.post("/auth/verify-otp-email/", {"phone": self.phone, "email": self.email, "code": self.code(), "new_password": "New-password-821!", "confirm_password": "different"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(EmailChallenge.objects.exists())
        user.refresh_from_db()
        self.assertTrue(user.check_password("Old-password-729!"))

    def test_signup_set_password_then_login(self):
        self.request_code()
        payload = self.verify().data
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + payload["access"])
        response = self.client.post("/set-password/", {"new_password": "New-password-821!", "confirm_password": "New-password-821!"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials()
        response = self.client.post("/login-phone/", {"phone_number": self.phone, "password": "New-password-821!"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)

    def test_existing_unverified_account_can_still_login_with_password(self):
        self.account(verified=False)
        response = self.client.post("/login-phone/", {"phone_number": self.phone, "password": "Old-password-729!"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_inactive_account_cannot_reset(self):
        user = self.account()
        self.request_code("reset")
        user.is_active = False
        user.save(update_fields=["is_active"])
        response = self.client.post("/auth/verify-otp-email/", {"phone": self.phone, "email": self.email, "code": self.code(), "new_password": "New-password-821!", "confirm_password": "New-password-821!"}, format="json")
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Old-password-729!"))

    def test_signup_code_rejected_for_other_phone(self):
        self.request_code()
        response = self.client.post("/auth/verify-signup-email/", {"phone": "+241074562848", "email": self.email, "code": self.code()}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CustomUser.objects.exists())

    def test_smtp_failure_returns_safe_message_and_allows_retry(self):
        with patch("users.email_otp.send_mail", side_effect=SMTPAuthenticationError(535, b"private-provider-detail")):
            with self.assertLogs("users.email_otp", level="ERROR") as logs:
                response = self.request_code()
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporairement indisponible", str(response.data["detail"]))
        self.assertNotIn("private-provider-detail", str(response.data))
        self.assertNotIn("private-provider-detail", " ".join(logs.output))
        self.assertFalse(EmailChallenge.objects.exists())
        self.assertEqual(self.request_code().status_code, 200)

    def test_timeout_returns_unavailable_and_rolls_back(self):
        with patch("users.email_otp.send_mail", side_effect=TimeoutError("timeout")):
            self.assertEqual(self.request_code().status_code, 503)
        self.assertFalse(EmailChallenge.objects.exists())

    def test_zero_sent_is_not_reported_as_success(self):
        with patch("users.email_otp.send_mail", return_value=0):
            self.assertEqual(self.request_code().status_code, 503)
        self.assertFalse(EmailChallenge.objects.exists())

    def test_failed_resend_preserves_previous_valid_code(self):
        self.request_code()
        old_code = self.code()
        EmailChallenge.objects.update(created_at=timezone.now() - timedelta(seconds=61))
        previous = EmailChallenge.objects.get()
        with patch("users.email_otp.send_mail", side_effect=SMTPServerDisconnected("closed")):
            self.assertEqual(self.request_code().status_code, 503)
        current = EmailChallenge.objects.get()
        self.assertEqual(current.code_digest, previous.code_digest)
        self.assertEqual(current.created_at, previous.created_at)
        self.assertEqual(self.verify(old_code).status_code, 200)
