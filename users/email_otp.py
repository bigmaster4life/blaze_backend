"""Email verification is separate from proof of phone ownership."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.utils.crypto import salted_hmac, constant_time_compare
from rest_framework.exceptions import ValidationError

from .models import CustomUser, EmailChallenge
from .utils import normalize_phone_gabon


def normalize_identity(phone, email):
    phone = normalize_phone_gabon(phone or "")
    email = (email or "").strip().lower()
    if not phone or not email:
        raise ValidationError("Numéro et email requis.")
    return phone, email


def eligible_user(phone, email, purpose):
    user = CustomUser.objects.filter(phone_number=phone).first()
    if purpose not in ("signup", "reset"):
        raise ValidationError("Usage OTP invalide.")
    if user:
        if not user.is_active or not user.email_verified or user.email.lower() != email:
            raise ValidationError("Ce numéro ne peut pas utiliser cette adresse pour recevoir un code.")
        if purpose == "signup" and user.has_usable_password():
            raise ValidationError("Ce compte existe déjà. Connectez-vous ou utilisez mot de passe oublié.")
    elif purpose == "reset":
        raise ValidationError("Ce numéro ne peut pas utiliser cette adresse pour recevoir un code.")
    if not user and CustomUser.objects.filter(email__iexact=email).exists():
        raise ValidationError("Cette adresse est déjà associée à un compte.")
    return user


def digest(phone, email, purpose, code):
    return salted_hmac("blaze.email-otp", f"{phone}|{email}|{purpose}|{code}", algorithm="sha256").hexdigest()


@transaction.atomic
def issue_code(phone, email, purpose):
    phone, email = normalize_identity(phone, email)
    eligible_user(phone, email, purpose)
    challenge, created = EmailChallenge.objects.get_or_create(
        phone_number=phone, purpose=purpose,
        defaults={"email": email, "code_digest": "", "expires_at": timezone.now()},
    )
    challenge = EmailChallenge.objects.select_for_update().get(pk=challenge.pk)
    now = timezone.now()
    if not created and challenge.created_at > now - timedelta(seconds=60):
        raise ValidationError("Attendez une minute avant de demander un nouveau code.")
    code = f"{secrets.randbelow(1000000):06d}"
    challenge.email = email
    challenge.code_digest = digest(phone, email, purpose, code)
    challenge.attempts = 0
    challenge.created_at = now
    challenge.expires_at = now + timedelta(minutes=5)
    challenge.save()
    send_mail("Votre code Blaze", f"Votre code Blaze est : {code}. Il est valable 5 minutes.",
              getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@blaze.com"), [email], fail_silently=False)


def complete_code(phone_number, email, purpose, code, new_password=None):
    phone, email = normalize_identity(phone_number, email)
    # Invalid attempts must commit even though the API returns a validation error.
    error = None
    user = None
    try:
        with transaction.atomic():
            challenge = EmailChallenge.objects.select_for_update().filter(phone_number=phone, purpose=purpose).first()
            if not challenge or challenge.email != email or challenge.expires_at <= timezone.now() or challenge.attempts >= 5:
                error = "Code invalide ou expiré."
            elif not constant_time_compare(challenge.code_digest, digest(phone, email, purpose, str(code))):
                challenge.attempts += 1
                challenge.save(update_fields=["attempts"])
                error = "Code invalide ou expiré."
            else:
                user = eligible_user(phone, email, purpose)
                if user:
                    user = CustomUser.objects.select_for_update().get(pk=user.pk)
                    # Recheck eligibility after locking the account.
                    if not user.is_active or not user.email_verified or user.email.lower() != email:
                        raise ValidationError("Compte non éligible.")
                    if purpose == "signup" and user.has_usable_password():
                        raise ValidationError("Ce compte existe déjà.")
                else:
                    user = CustomUser(phone_number=phone, email=email, email_verified=True, user_type="client")
                    user.set_unusable_password()
                    user.save()
                if purpose == "reset":
                    if not new_password:
                        raise ValidationError("Nouveau mot de passe requis.")
                    user.set_password(new_password)
                    user.save(update_fields=["password"])
                challenge.delete()
    except IntegrityError:
        raise ValidationError("Ce numéro ou cet email est déjà associé à un compte.")
    if error:
        raise ValidationError(error)
    return user
