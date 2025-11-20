import asyncio
import time
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)

async def _presence_touch(driver_id: int):
    """
    Marque un chauffeur comme actif (dernière activité).
    Sert pour le tracking de présence WebSocket (ping/pong).
    """
    key = f"driver:{driver_id}:last_seen"
    ts = int(time.time())
    cache.set(key, ts, timeout=600)  # expire après 10 minutes
    logger.debug(f"[PRESENCE] touch driver#{driver_id} at {ts}")
    # Optionnel : petit délai pour ne pas bloquer le loop
    await asyncio.sleep(0)