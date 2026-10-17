"""Best-effort push fallback; persisted delivery status and WebSocket remain authoritative."""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from django.db import close_old_connections

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='delivery-push')
_slots = BoundedSemaphore(64)


def _send_status(request_id, expected_status):
    close_old_connections()
    try:
        from Delivery.models import DeliveryRequest
        from notifications.fcm import notify_client_update
        req = DeliveryRequest.objects.select_related('user').filter(pk=request_id).first()
        if req is not None and req.status == expected_status:
            notify_client_update(req.user, 'delivery', req.id, expected_status)
    except Exception:
        logger.exception('Delivery push failed request=%s', request_id)
    finally:
        close_old_connections()


def submit_delivery_status(request_id, status):
    if not _slots.acquire(blocking=False):
        logger.warning('Delivery push queue full request=%s; WebSocket/polling remain active', request_id)
        return False
    try:
        future = _executor.submit(_send_status, request_id, status)
        future.add_done_callback(lambda _: _slots.release())
        return True
    except Exception:
        _slots.release()
        logger.exception('Delivery push submission failed request=%s', request_id)
        return False
