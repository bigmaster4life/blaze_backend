from channels.db import database_sync_to_async


def authentication_error(user, expected_id=None):
    if not user or not user.is_authenticated or not user.is_active:
        return 4401
    if expected_id is not None and user.pk != expected_id:
        return 4403
    return None


@database_sync_to_async
def driver_allowed(user_id, delivery=False):
    if delivery:
        from Delivery.models import DeliveryDriver
        return DeliveryDriver.objects.filter(user_id=user_id, is_blocked=False).exists()
    from drivers.models import Driver
    return Driver.objects.filter(user_id=user_id, is_blocked=False).exists()
