"""Durable pending flags survive process restarts; new events and the review UI drain them."""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from django.db import close_old_connections

logger=logging.getLogger(__name__)
_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cancellation-screening')
_guard=Lock()


def _run():
    close_old_connections()
    try:
        from .detection import process_pending
        process_pending(50)
    except Exception:
        logger.exception('Cancellation screening failed; pending records retained for retry')
    finally:
        close_old_connections()
        _guard.release()


def schedule_detection():
    if not _guard.acquire(blocking=False):
        return False
    try:
        _executor.submit(_run)
        return True
    except Exception:
        _guard.release()
        logger.exception('Cancellation screening unavailable; records remain pending')
        return False
