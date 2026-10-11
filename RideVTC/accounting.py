"""Freeze allocations for successful payments on server-quoted rides only."""
from decimal import Decimal
from django.db.models import F, Value, DecimalField
from django.db.models.functions import Coalesce
from .models import RidePricing, PaymentAllocation
from .pricing import split_amount


def record_allocation(payment):
    if payment.status != 'SUCCESS':
        return None
    pricing = RidePricing.objects.filter(ride_id=payment.ride_id).first()
    if not pricing:
        return None  # Never reinterpret historical payments as new 22% payments.
    amounts = split_amount(payment.amount, pricing.commission_rate)
    allocation, _ = PaymentAllocation.objects.get_or_create(payment=payment, defaults={
        **amounts, 'commission_rate': pricing.commission_rate,
        'cash_commission_due': amounts['platform_amount'] if payment.wallet == 'CASH' else 0,
    })
    return allocation


def allocated_amount(field, legacy_fraction):
    """Preserve each caller's historical calculation when no allocation exists."""
    money = DecimalField(max_digits=12, decimal_places=2)
    return Coalesce(F('allocation__' + field), F('amount') * Value(Decimal(str(legacy_fraction))), output_field=money)
