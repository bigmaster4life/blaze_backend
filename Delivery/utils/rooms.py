# Delivery/utils/rooms.py
import re

def _slug(x: str) -> str:
    x = (x or "").strip().lower()
    x = re.sub(r"[^0-9A-Za-z._-]", "_", x)
    return x[:50] or "default"

def delivery_driver_room(user_id: int | str) -> str:
    return f"delivery.driver.{int(user_id)}"

def delivery_pool_room(city: str, vehicle_type: str) -> str:
    return f"delivery.pool.{_slug(city)}.{_slug(vehicle_type)}"

def delivery_user_room(user_id: int | str) -> str:
    return f"delivery.user.{int(user_id)}"