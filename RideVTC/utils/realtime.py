# RideVTC/utils/realtime.py
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

log = logging.getLogger(__name__)
layer = get_channel_layer()

def emit_to_group(group: str, event: str, payload: dict):
    log.info("EMIT %s → %s : %s", event, group, payload)
    async_to_sync(layer.group_send)(
        group,
        {"type": "evt", "event": event, "payload": payload},
    )