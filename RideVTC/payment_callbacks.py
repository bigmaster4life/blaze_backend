"""Callback identity and state rules, shared by rental and ride payments."""


class CallbackIdentityConflict(Exception):
    pass


def locked_payment(model, provider_txid, reference):
    """Must run inside transaction.atomic; never match a missing transaction ID."""
    payment = None
    if provider_txid:
        matches = list(model.objects.select_for_update().filter(provider_txid=provider_txid)[:2])
        if len(matches) > 1:
            raise CallbackIdentityConflict()
        if matches:
            payment = matches[0]
            if reference and payment.idempotency_key != reference:
                raise CallbackIdentityConflict()
    if payment is None and reference:
        payment = model.objects.select_for_update().filter(idempotency_key=reference).first()
        if payment and provider_txid and payment.provider_txid and payment.provider_txid not in (provider_txid, payment.idempotency_key):
            raise CallbackIdentityConflict()
    return payment


def update_payment(payment, new_status, provider_txid):
    """Success is final; a delayed pending event cannot undo a failure either.

    A later success can supersede a failure (e.g. an uncertain initiation timeout).
    Returns True only when the payment status actually changed.
    """
    fields = []
    if provider_txid and (not payment.provider_txid or payment.provider_txid == payment.idempotency_key):
        payment.provider_txid = provider_txid
        fields.append("provider_txid")
    changed = (
        payment.status != new_status
        and payment.status != "SUCCESS"
        and not (payment.status == "FAILED" and new_status == "PENDING")
    )
    if changed:
        payment.status = new_status
        fields.append("status")
    if fields:
        payment.save(update_fields=fields + ["updated_at"])
    return changed
