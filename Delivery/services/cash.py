from django.utils import timezone
from rest_framework.exceptions import ValidationError


def purchases(req):
    details = req.order_details or {}
    return details if details.get('purchase_mode') == 'buy_for_me' else None


def amount(value, label='Montant'):
    if isinstance(value, bool):
        raise ValidationError({'detail': f'{label} invalide.'})
    try:
        parsed = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValidationError({'detail': f'{label} : indiquez un montant entier en FCFA.'}) from exc
    if str(parsed) != str(value).strip() or not 1 <= parsed <= 1000000:
        raise ValidationError({'detail': f'{label} : montant entier entre 1 et 1 000 000 FCFA.'})
    return parsed


def fee(req):
    value = req.final_price if req.final_price is not None else req.estimated_price
    if not value or value < 1:
        raise ValidationError({'detail': 'Le tarif de livraison doit être fixé avant l’encaissement.'})
    return value


def check_pickup_payment(req):
    if req.payment_payer == 'sender' and not req.is_paid:
        raise ValidationError({'detail': 'Confirmez les frais de livraison reçus de l’expéditeur avant de partir.'})
    details = purchases(req)
    if not details:
        return
    if req.purchase_actual_amount is None:
        raise ValidationError({'detail': 'Enregistrez le montant réel des achats avant de confirmer le retrait.'})
    if details.get('funding_mode', 'client_funded') == 'client_funded':
        if not req.purchase_funds_received_at or (req.purchase_funds_amount or 0) < req.purchase_actual_amount:
            raise ValidationError({'detail': 'Les fonds du client doivent être reçus et couvrir les achats.'})
    elif not req.purchase_advance_accepted_at:
        raise ValidationError({'detail': 'L’avance des achats doit avoir été acceptée par le livreur.'})
    elif req.payment_payer == 'sender' and not req.purchase_reimbursed_at:
        raise ValidationError({'detail': 'Confirmez le remboursement des achats par l’expéditeur avant le départ.'})


def record_purchase_amount(req, value):
    details = purchases(req)
    if not details:
        raise ValidationError({'detail': 'Cette commande ne nécessite pas d’achat par le livreur.'})
    actual = amount(value, 'Montant réel des achats')
    if req.purchase_reimbursed_at and actual != req.purchase_actual_amount:
        raise ValidationError({'detail': 'Les achats ont déjà été remboursés ; leur montant ne peut plus être modifié.'})
    if actual > int(details.get('budget') or 0):
        raise ValidationError({'detail': 'Le montant des achats dépasse le budget autorisé.'})
    if details.get('funding_mode', 'client_funded') == 'client_funded':
        if not req.purchase_funds_received_at or actual > (req.purchase_funds_amount or 0):
            raise ValidationError({'detail': 'Confirmez d’abord les fonds reçus du client ; ils doivent couvrir les achats.'})
    elif not req.purchase_advance_accepted_at:
        raise ValidationError({'detail': 'L’avance des achats n’a pas été acceptée.'})
    req.purchase_actual_amount = actual
    req.save(update_fields=['purchase_actual_amount', 'updated_at'])


def confirm_change(req, confirmed):
    details = purchases(req)
    if details and details.get('funding_mode', 'client_funded') == 'client_funded':
        change = (req.purchase_funds_amount or 0) - (req.purchase_actual_amount or 0)
        if change > 0:
            if confirmed is not True:
                raise ValidationError({'detail': f'Confirmez la restitution de {change} FCFA de monnaie au client.'})
            req.purchase_change_returned_at = timezone.now()
