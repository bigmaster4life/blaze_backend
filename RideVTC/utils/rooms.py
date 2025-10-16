# RideVTC/utils/rooms.py
import re

def _slug(x: str) -> str:
    x = (x or "").strip().lower()
    x = re.sub(r"[^0-9A-Za-z._-]", "_", x)
    return x[:50] or "default"

def pool_room(category: str, area: str) -> str:
    return f"pool.{_slug(category)}.{_slug(area)}"

def user_room(user_id: int | str) -> str:
    return f"user.{int(user_id)}"

def driver_room(driver_id: int | str) -> str:
    return f"driver.{int(driver_id)}"