"""Delivery fee allocation. Purchase reimbursements are never commissionable."""
from decimal import Decimal
from django.db.models import Sum
from RideVTC.pricing import split_amount
from Delivery.models import DeliveryPaymentAllocation

COMMISSION_RATE = Decimal('0.22')


def record_cash_allocation(req, driver, paid_amount):
    # Called within the request/driver row locks used for cash confirmation.
    amounts = split_amount(paid_amount, COMMISSION_RATE)
    allocation, created = DeliveryPaymentAllocation.objects.get_or_create(
        request=req,
        defaults={
            'driver': driver,
            'commission_rate': COMMISSION_RATE,
            **amounts,
            'cash_commission_due': amounts['platform_amount'],
        },
    )
    if created:
        driver.total_earnings += allocation.driver_amount
        driver.save(update_fields=['total_earnings', 'updated_at'])
    return allocation


def driver_summary(driver):
    totals = DeliveryPaymentAllocation.objects.filter(driver=driver).aggregate(
        gross=Sum('paid_amount'), net=Sum('driver_amount'),
        commission=Sum('platform_amount'), cash_commission_due=Sum('cash_commission_due'),
    )
    return {
        **{key: str(value or 0) for key, value in totals.items()},
        'commission_rate': str(COMMISSION_RATE),
        'scope': 'recorded_allocations_only',
        'withdrawable_balance': None,
    }
